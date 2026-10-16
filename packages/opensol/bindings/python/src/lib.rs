//! Shared Python parameter conversion, blocking wrappers, and native async wrappers.

mod aio;
mod resources;

use std::{
    collections::BTreeMap,
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use futures::{
    StreamExt,
    future::{AbortHandle, Abortable},
};
use opensol_adapters::{
    agent_sandbox::{AgentSandboxConfig, AgentSandboxLifecycle},
    docker::{DockerConfig, DockerConnection, DockerLifecycle},
    e2b::{E2bConfig, E2bLifecycle},
    envd::{Envd, EnvdConfig},
    grpc::GrpcConfig,
    local::Local,
    openshell::{
        OpenShellConnection, OpenShellCreate, OpenShellLifecycle, protocol as openshell_protocol,
    },
    pueue::{Pueue, PueueConfig},
    sandboxd::Sandboxd,
    secret::Secret,
    ssh::{Ssh, SshConfig},
};
use opensol_core::{
    Command, Connection, CreateOptions, Error, ErrorKind, EventStream, ExecRequest, ExecResult,
    ExitStatus, Lifecycle, OutputChannel, ProcessEvent, ProcessHandle, ProcessInfo, ProcessManager,
    ProcessOutcome, ProcessRef, PtySize, ResourceInfo, ResourceRef, Result, Signal, SpawnRequest,
};
use prost::Message;
use pyo3::{
    create_exception,
    exceptions::PyException,
    prelude::*,
    types::{PyBytes, PyDict},
};
use tokio::runtime::Runtime;

create_exception!(opensol, SdkError, PyException);
create_exception!(opensol, InvalidArgumentError, SdkError);
create_exception!(opensol, UnsupportedError, SdkError);
create_exception!(opensol, NotFoundError, SdkError);
create_exception!(opensol, ConflictError, SdkError);
create_exception!(opensol, PermissionError, SdkError);
create_exception!(opensol, TimeoutError, SdkError);
create_exception!(opensol, TransportError, SdkError);
create_exception!(opensol, BackendError, SdkError);

fn exception(error: Error) -> PyErr {
    let message = error.to_string();
    let exception = match error.kind {
        ErrorKind::InvalidArgument => InvalidArgumentError::new_err(message),
        ErrorKind::Unsupported => UnsupportedError::new_err(message),
        ErrorKind::NotFound => NotFoundError::new_err(message),
        ErrorKind::Conflict => ConflictError::new_err(message),
        ErrorKind::Permission => PermissionError::new_err(message),
        ErrorKind::Timeout => TimeoutError::new_err(message),
        ErrorKind::Transport => TransportError::new_err(message),
        ErrorKind::Backend => BackendError::new_err(message),
    };
    Python::attach(|py| {
        let value = exception.value(py);
        let _ = value.setattr("backend", error.backend);
        let _ = value.setattr("operation", error.operation);
        let _ = value.setattr("kind", format!("{:?}", error.kind));
    });
    exception
}

fn runtime() -> PyResult<&'static Runtime> {
    static RUNTIME: OnceLock<std::result::Result<Runtime, Error>> = OnceLock::new();
    RUNTIME
        .get_or_init(|| {
            tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .map_err(|_| {
                    Error::new(
                        ErrorKind::Backend,
                        "python",
                        "runtime",
                        "could not initialize SDK I/O runtime",
                    )
                })
        })
        .as_ref()
        .map_err(|error| exception(error.clone()))
}

fn timeout(value: Option<f64>) -> PyResult<Option<Duration>> {
    value
        .map(|value| {
            Duration::try_from_secs_f64(value)
                .ok()
                .filter(|duration| !duration.is_zero())
                .ok_or_else(|| {
                    exception(Error::invalid(
                        "python",
                        "timeout",
                        "timeout must be finite and positive, or None",
                    ))
                })
        })
        .transpose()
}

fn command(
    argv: Vec<String>,
    cwd: Option<String>,
    env: Option<BTreeMap<String, String>>,
) -> Command {
    Command {
        argv,
        cwd,
        env: env.unwrap_or_default(),
    }
}

fn portable_signal(signal: i32, backend: &str) -> PyResult<Signal> {
    match signal {
        2 => Ok(Signal::Interrupt),
        9 => Ok(Signal::Kill),
        15 => Ok(Signal::Terminate),
        _ => Err(exception(Error::invalid(
            backend,
            "signal",
            "supported signals are 2, 9 and 15",
        ))),
    }
}

fn capabilities<'py>(
    py: Python<'py>,
    value: impl serde::Serialize,
) -> PyResult<Bound<'py, PyDict>> {
    let value = serde_json::to_value(value).map_err(|_| {
        exception(Error::new(
            ErrorKind::Backend,
            "python",
            "capabilities",
            "capability conversion failed",
        ))
    })?;
    let result = PyDict::new(py);
    if let Some(fields) = value.as_object() {
        for (key, value) in fields {
            match value {
                serde_json::Value::Bool(value) => result.set_item(key, value)?,
                serde_json::Value::Array(values) if key == "supported_signals" => {
                    let numbers: Vec<i32> = values
                        .iter()
                        .map(|value| match value.as_str() {
                            Some("Interrupt") => Ok(2),
                            Some("Terminate") => Ok(15),
                            Some("Kill") => Ok(9),
                            _ => Err(exception(Error::new(
                                ErrorKind::Backend,
                                "python",
                                "capabilities",
                                "unknown portable signal",
                            ))),
                        })
                        .collect::<PyResult<_>>()?;
                    result.set_item(key, numbers)?;
                }
                _ => {
                    return Err(exception(Error::new(
                        ErrorKind::Backend,
                        "python",
                        "capabilities",
                        "unsupported capability value",
                    )));
                }
            }
        }
    }
    Ok(result)
}

#[pyclass(name = "ResourceRef", frozen)]
#[derive(Clone)]
struct PyResourceRef {
    inner: ResourceRef,
}

#[pymethods]
impl PyResourceRef {
    #[new]
    fn new(backend: String, id: String) -> Self {
        Self {
            inner: ResourceRef::new(&backend, id),
        }
    }
    #[getter]
    fn backend(&self) -> &str {
        &self.inner.backend
    }
    #[getter]
    fn id(&self) -> &str {
        &self.inner.id
    }
    fn __repr__(&self) -> String {
        format!("ResourceRef({:?}, {:?})", self.inner.backend, self.inner.id)
    }
}

#[pyclass(name = "ProcessRef", frozen)]
#[derive(Clone)]
struct PyProcessRef {
    inner: ProcessRef,
}

#[pymethods]
impl PyProcessRef {
    #[new]
    #[pyo3(signature = (backend, id, pty=None))]
    fn new(backend: String, id: String, pty: Option<bool>) -> Self {
        Self {
            inner: ProcessRef::new(&backend, id, pty),
        }
    }
    #[getter]
    fn backend(&self) -> &str {
        &self.inner.backend
    }
    #[getter]
    fn id(&self) -> &str {
        &self.inner.id
    }
    #[getter]
    fn pty(&self) -> Option<bool> {
        self.inner.pty
    }
    fn __repr__(&self) -> String {
        format!(
            "ProcessRef({:?}, {:?}, pty={})",
            self.inner.backend,
            self.inner.id,
            match self.inner.pty {
                Some(true) => "True",
                Some(false) => "False",
                None => "None",
            }
        )
    }
}

#[pyclass(name = "ExitStatus", frozen)]
#[derive(Clone)]
struct PyExitStatus {
    inner: ExitStatus,
}

#[pymethods]
impl PyExitStatus {
    #[getter]
    fn code(&self) -> Option<i32> {
        self.inner.code
    }
    #[getter]
    fn signal(&self) -> Option<i32> {
        self.inner.signal
    }
    #[getter]
    fn success(&self) -> bool {
        self.inner.success()
    }
    fn __repr__(&self) -> String {
        format!(
            "ExitStatus(code={:?}, signal={:?})",
            self.inner.code, self.inner.signal
        )
    }
}

/// Shared terminal task value; a native result need not include an OS exit status.
#[pyclass(name = "ProcessOutcome", frozen)]
#[derive(Clone)]
struct PyProcessOutcome {
    inner: ProcessOutcome,
}

#[pymethods]
impl PyProcessOutcome {
    #[getter]
    fn exit(&self) -> Option<PyExitStatus> {
        self.inner.exit.map(|inner| PyExitStatus { inner })
    }
    #[getter]
    fn native_result(&self) -> Option<&str> {
        self.inner.native_result.as_deref()
    }
    fn __repr__(&self) -> String {
        format!(
            "ProcessOutcome(exit={:?}, native_result={:?})",
            self.inner.exit, self.inner.native_result
        )
    }
}

#[pyclass(name = "ExecResult", frozen)]
struct PyExecResult {
    inner: ExecResult,
}

#[pymethods]
impl PyExecResult {
    #[getter]
    fn stdout<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.stdout)
    }
    #[getter]
    fn stderr<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.stderr)
    }
    #[getter]
    fn exit(&self) -> PyExitStatus {
        PyExitStatus {
            inner: self.inner.exit,
        }
    }
    #[getter]
    fn exit_code(&self) -> Option<i32> {
        self.inner.exit.code
    }
    #[getter]
    fn signal(&self) -> Option<i32> {
        self.inner.exit.signal
    }
    #[getter]
    fn success(&self) -> bool {
        self.inner.exit.success()
    }
}

#[pyclass(name = "ResourceInfo", frozen)]
struct PyResourceInfo {
    inner: ResourceInfo,
}

#[pymethods]
impl PyResourceInfo {
    #[getter]
    fn resource(&self) -> PyResourceRef {
        PyResourceRef {
            inner: self.inner.resource.clone(),
        }
    }
    #[getter]
    fn state(&self) -> String {
        serde_json::to_value(self.inner.state)
            .expect("state is serializable")
            .as_str()
            .unwrap_or("unknown")
            .into()
    }
    #[getter]
    fn native_state(&self) -> &str {
        &self.inner.native_state
    }
}

#[pyclass(name = "ProcessInfo", frozen)]
struct PyProcessInfo {
    inner: ProcessInfo,
}

#[pymethods]
impl PyProcessInfo {
    #[getter]
    fn process(&self) -> PyProcessRef {
        PyProcessRef {
            inner: self.inner.process.clone(),
        }
    }
    #[getter]
    fn state(&self) -> String {
        serde_json::to_value(self.inner.state)
            .expect("state is serializable")
            .as_str()
            .unwrap_or("unknown")
            .into()
    }
    #[getter]
    fn native_state(&self) -> &str {
        &self.inner.native_state
    }
    #[getter]
    fn outcome(&self) -> Option<PyProcessOutcome> {
        self.inner
            .outcome
            .clone()
            .map(|inner| PyProcessOutcome { inner })
    }
}

/// Blocking completed-command methods release the Python GIL during all Rust I/O.
#[pyclass(name = "Connection", frozen)]
#[derive(Clone)]
struct PyConnection {
    inner: Connection,
}

#[pymethods]
impl PyConnection {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    #[pyo3(signature = (argv, *, cwd=None, env=None, stdin=b"".as_slice(), timeout=None))]
    fn exec(
        &self,
        py: Python<'_>,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: &[u8],
        timeout: Option<f64>,
    ) -> PyResult<PyExecResult> {
        let request = ExecRequest {
            command: command(argv, cwd, env),
            stdin: stdin.to_vec(),
            timeout: crate::timeout(timeout)?,
        };
        let connection = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(connection.exec(request)))
            .map(|inner| PyExecResult { inner })
            .map_err(exception)
    }

    #[pyo3(signature = (script, *, cwd=None, env=None, stdin=b"".as_slice(), timeout=None))]
    fn shell(
        &self,
        py: Python<'_>,
        script: String,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: &[u8],
        timeout: Option<f64>,
    ) -> PyResult<PyExecResult> {
        self.exec(py, Command::shell(script).argv, cwd, env, stdin, timeout)
    }
}

/// Closing/dropping this wrapper never deletes native environments.
#[pyclass(name = "Lifecycle", frozen)]
struct PyLifecycle {
    inner: Lifecycle,
}

#[pymethods]
impl PyLifecycle {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    #[pyo3(signature = (*, name=None, labels=None))]
    fn create(
        &self,
        py: Python<'_>,
        name: Option<String>,
        labels: Option<BTreeMap<String, String>>,
    ) -> PyResult<PyResourceRef> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || {
            runtime.block_on(client.create(CreateOptions {
                name,
                labels: labels.unwrap_or_default(),
            }))
        })
        .map(|inner| PyResourceRef { inner })
        .map_err(exception)
    }

    fn inspect(
        &self,
        py: Python<'_>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<PyResourceInfo> {
        let resource = resource.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.inspect(&resource)))
            .map(|inner| PyResourceInfo { inner })
            .map_err(exception)
    }

    fn start(&self, py: Python<'_>, resource: PyRef<'_, PyResourceRef>) -> PyResult<()> {
        let resource = resource.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.start(&resource)))
            .map_err(exception)
    }

    fn stop(&self, py: Python<'_>, resource: PyRef<'_, PyResourceRef>) -> PyResult<()> {
        let resource = resource.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.stop(&resource)))
            .map_err(exception)
    }

    fn delete(&self, py: Python<'_>, resource: PyRef<'_, PyResourceRef>) -> PyResult<()> {
        let resource = resource.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.delete(&resource)))
            .map_err(exception)
    }

    fn fork(&self, py: Python<'_>, resource: PyRef<'_, PyResourceRef>) -> PyResult<PyResourceRef> {
        let resource = resource.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.fork(&resource)))
            .map(|inner| PyResourceRef { inner })
            .map_err(exception)
    }
}

#[pyclass(name = "ProcessEvent", frozen)]
struct PyProcessEvent {
    inner: ProcessEvent,
}

#[pymethods]
impl PyProcessEvent {
    #[getter]
    fn channel(&self) -> Option<&'static str> {
        match &self.inner {
            ProcessEvent::Output { channel, .. } => Some(match channel {
                OutputChannel::Stdout => "stdout",
                OutputChannel::Stderr => "stderr",
                OutputChannel::Terminal => "terminal",
                OutputChannel::Combined => "combined",
            }),
            _ => None,
        }
    }
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(
            py,
            match &self.inner {
                ProcessEvent::Output { data, .. } => data,
                _ => &[],
            },
        )
    }
    #[getter]
    fn outcome(&self) -> Option<PyProcessOutcome> {
        match &self.inner {
            ProcessEvent::Completed(inner) => Some(PyProcessOutcome {
                inner: inner.clone(),
            }),
            _ => None,
        }
    }
}

struct HandleState {
    events: Option<EventStream>,
    // Completion belongs to this subscription's execution, even if its task ID is restarted.
    outcome: Option<ProcessOutcome>,
}

/// One output subscription. disconnect releases it without killing the remote process.
#[pyclass(name = "ProcessHandle", frozen)]
struct PyProcessHandle {
    process: ProcessRef,
    state: Arc<Mutex<HandleState>>,
    abort: AbortHandle,
    disconnected: Arc<AtomicBool>,
}

impl From<ProcessHandle> for PyProcessHandle {
    fn from(handle: ProcessHandle) -> Self {
        let (abort, registration) = AbortHandle::new_pair();
        Self {
            process: handle.process,
            state: Arc::new(Mutex::new(HandleState {
                events: Some(Box::pin(Abortable::new(handle.events, registration))),
                outcome: None,
            })),
            abort,
            disconnected: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[pymethods]
impl PyProcessHandle {
    #[getter]
    fn process(&self) -> PyProcessRef {
        PyProcessRef {
            inner: self.process.clone(),
        }
    }

    #[pyo3(signature = (*, timeout=None))]
    fn next_event(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<PyProcessEvent>> {
        let timeout = crate::timeout(timeout)?;
        let state = self.state.clone();
        let backend = self.process.backend.clone();
        let disconnected = self.disconnected.clone();
        let runtime = runtime()?;
        py.detach(move || -> Result<Option<ProcessEvent>> {
            let mut state = state.lock().map_err(|_| {
                Error::new(
                    ErrorKind::Backend,
                    &backend,
                    "output",
                    "output subscription lock failed",
                )
            })?;
            let Some(events) = state.events.as_mut() else {
                return Ok(None);
            };
            let event =
                runtime.block_on(async {
                    match timeout {
                        Some(timeout) => tokio::time::timeout(timeout, events.next())
                            .await
                            .map_err(|_| {
                                Error::new(
                                    ErrorKind::Timeout,
                                    &backend,
                                    "output",
                                    "output wait timed out",
                                )
                            }),
                        None => Ok(events.next().await),
                    }
                })?;
            match event {
                Some(Ok(event)) => {
                    if let ProcessEvent::Completed(outcome) = &event {
                        state.outcome = Some(outcome.clone());
                        state.events = None;
                    }
                    Ok(Some(event))
                }
                Some(Err(error)) => {
                    state.events = None;
                    Err(error)
                }
                None => {
                    state.events = None;
                    if state.outcome.is_none() && !disconnected.load(Ordering::Acquire) {
                        Err(Error::new(
                            ErrorKind::Transport,
                            &backend,
                            "output",
                            "subscription ended before process completion",
                        ))
                    } else {
                        Ok(None)
                    }
                }
            }
        })
        .map(|event| event.map(|inner| PyProcessEvent { inner }))
        .map_err(exception)
    }

    fn wait(&self, py: Python<'_>) -> PyResult<PyProcessOutcome> {
        loop {
            let state = self.state.clone();
            let backend = self.process.backend.clone();
            let outcome = py.detach(move || {
                state
                    .lock()
                    .map(|state| state.outcome.clone())
                    .map_err(|_| {
                        exception(Error::new(
                            ErrorKind::Backend,
                            &backend,
                            "wait",
                            "output subscription lock failed",
                        ))
                    })
            })?;
            if let Some(outcome) = outcome {
                return Ok(PyProcessOutcome { inner: outcome });
            }
            if self.next_event(py, None)?.is_none() {
                return Err(exception(Error::new(
                    ErrorKind::Transport,
                    &self.process.backend,
                    "wait",
                    "subscription is disconnected; completion is unknown",
                )));
            }
        }
    }

    fn disconnect(&self, py: Python<'_>) -> PyResult<()> {
        self.disconnected.store(true, Ordering::Release);
        self.abort.abort();
        let state = self.state.clone();
        let backend = self.process.backend.clone();
        py.detach(move || {
            state
                .lock()
                .map_err(|_| {
                    exception(Error::new(
                        ErrorKind::Backend,
                        &backend,
                        "disconnect",
                        "output subscription lock failed",
                    ))
                })?
                .events = None;
            Ok(())
        })
    }
}

#[pyclass(name = "ProcessManager", frozen)]
#[derive(Clone)]
struct PyProcessManager {
    inner: ProcessManager,
}

#[pymethods]
impl PyProcessManager {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    fn get(&self, process: PyRef<'_, PyProcessRef>) -> resources::PyProcess {
        resources::PyProcess {
            inner: self.inner.get(process.inner.clone()),
        }
    }

    fn probe(&self, py: Python<'_>) -> PyResult<resources::PyManagerInfo> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.probe()))
            .map(|inner| resources::PyManagerInfo { inner })
            .map_err(exception)
    }

    fn list(&self, py: Python<'_>) -> PyResult<Vec<PyProcessInfo>> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.list()))
            .map(|list| {
                list.into_iter()
                    .map(|inner| PyProcessInfo { inner })
                    .collect()
            })
            .map_err(exception)
    }

    #[pyo3(signature = (argv, *, cwd=None, env=None, stdin=false, pty=None))]
    fn spawn(
        &self,
        py: Python<'_>,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: bool,
        pty: Option<(u32, u32)>,
    ) -> PyResult<PyProcessHandle> {
        let request = SpawnRequest {
            command: command(argv, cwd, env),
            stdin,
            pty: pty.map(|(cols, rows)| PtySize { cols, rows }),
        };
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.spawn(request)))
            .map(Into::into)
            .map_err(exception)
    }

    fn status(&self, py: Python<'_>, process: PyRef<'_, PyProcessRef>) -> PyResult<PyProcessInfo> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.status(&process)))
            .map(|inner| PyProcessInfo { inner })
            .map_err(exception)
    }

    #[pyo3(signature = (process, *, timeout=None))]
    fn wait(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
        timeout: Option<f64>,
    ) -> PyResult<PyProcessOutcome> {
        let process = process.inner.clone();
        let timeout = crate::timeout(timeout)?;
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.wait(&process, timeout)))
            .map(|inner| PyProcessOutcome { inner })
            .map_err(exception)
    }

    fn output(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<PyProcessHandle> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || {
            runtime.block_on(async {
                Ok(ProcessHandle {
                    events: client.output(&process).await?,
                    process,
                })
            })
        })
        .map(Into::into)
        .map_err(exception)
    }

    fn reconnect(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<PyProcessHandle> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.reconnect(&process)))
            .map(Into::into)
            .map_err(exception)
    }

    fn send_input(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
        data: &[u8],
    ) -> PyResult<()> {
        let process = process.inner.clone();
        let data = data.to_vec();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.send_input(&process, data)))
            .map_err(exception)
    }

    fn close_stdin(&self, py: Python<'_>, process: PyRef<'_, PyProcessRef>) -> PyResult<()> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.close_stdin(&process)))
            .map_err(exception)
    }

    fn signal(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
        signal: i32,
    ) -> PyResult<()> {
        let signal = portable_signal(signal, self.inner.backend())?;
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.signal(&process, signal)))
            .map_err(exception)
    }

    fn resize(
        &self,
        py: Python<'_>,
        process: PyRef<'_, PyProcessRef>,
        cols: u32,
        rows: u32,
    ) -> PyResult<()> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.resize(&process, PtySize { cols, rows })))
            .map_err(exception)
    }
}

/// One native envd context exposing two independent public clients.
#[pyclass(name = "Envd", frozen)]
struct PyEnvd {
    inner: Envd,
}

#[pymethods]
impl PyEnvd {
    #[getter]
    fn connection(&self) -> PyConnection {
        PyConnection {
            inner: self.inner.connection(),
        }
    }
    #[getter]
    fn processes(&self) -> PyProcessManager {
        PyProcessManager {
            inner: self.inner.processes(),
        }
    }
}

/// Provider context retained for lifecycle operations and local connection binding.
#[pyclass(name = "Docker", frozen)]
struct PyDocker {
    inner: DockerLifecycle,
}

#[pymethods]
impl PyDocker {
    #[getter]
    fn lifecycle(&self) -> PyLifecycle {
        PyLifecycle {
            inner: self.inner.lifecycle(),
        }
    }
    fn connection(&self, resource: PyRef<'_, PyResourceRef>) -> PyResult<PyConnection> {
        self.inner
            .connection(&resource.inner)
            .map(|inner| PyConnection { inner })
            .map_err(exception)
    }
}

/// E2B configuration and credentials are reused for read-only envd discovery.
#[pyclass(name = "E2b", frozen)]
struct PyE2b {
    inner: E2bLifecycle,
}

#[pymethods]
impl PyE2b {
    #[getter]
    fn lifecycle(&self) -> PyLifecycle {
        PyLifecycle {
            inner: self.inner.lifecycle(),
        }
    }
    fn envd(&self, py: Python<'_>, resource: PyRef<'_, PyResourceRef>) -> PyResult<PyEnvd> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.envd(&resource)))
            .map(|inner| PyEnvd { inner })
            .map_err(exception)
    }
    fn _aio_envd<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        pyo3_async_runtimes::tokio::future_into_py(py, async move {
            client
                .envd(&resource)
                .await
                .map(|inner| PyEnvd { inner })
                .map_err(exception)
        })
    }
}

/// Keeps the established gateway channel, credentials and workspace for child connections.
#[pyclass(name = "OpenShell", frozen)]
struct PyOpenShell {
    inner: OpenShellLifecycle,
}

#[pymethods]
impl PyOpenShell {
    #[getter]
    fn lifecycle(&self) -> PyLifecycle {
        PyLifecycle {
            inner: self.inner.lifecycle(),
        }
    }
    fn connection(&self, resource: PyRef<'_, PyResourceRef>) -> PyResult<PyConnection> {
        self.inner
            .connection(&resource.inner)
            .map(|inner| PyConnection { inner })
            .map_err(exception)
    }
}

#[pyfunction]
fn local() -> PyConnection {
    PyConnection {
        inner: Connection::new(Local),
    }
}

#[pyfunction]
#[pyo3(signature = (destination, *, port=None, identity_file=None, config_file=None, program="ssh"))]
fn ssh(
    destination: String,
    port: Option<u16>,
    identity_file: Option<String>,
    config_file: Option<String>,
    program: &str,
) -> PyResult<PyConnection> {
    let mut config = SshConfig::new(destination);
    config.port = port;
    config.identity_file = identity_file.map(Into::into);
    config.config_file = config_file.map(Into::into);
    config.program = program.into();
    Ssh::new(config)
        .map(|inner| PyConnection {
            inner: Connection::new(inner),
        })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (connection, *, config_file=None, profile=None, group=None, pueue_directory=None, program="pueue"))]
fn pueue(
    connection: PyRef<'_, PyConnection>,
    config_file: Option<String>,
    profile: Option<String>,
    group: Option<String>,
    pueue_directory: Option<String>,
    program: &str,
) -> PyResult<PyProcessManager> {
    let config = PueueConfig {
        config_file,
        profile,
        group,
        pueue_directory,
        program: program.into(),
        ..Default::default()
    };
    Pueue::new(connection.inner.clone(), config)
        .map(|inner| PyProcessManager {
            inner: ProcessManager::new(inner),
        })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (endpoint, *, access_token=None, user=None, sandbox_id=None, request_timeout=30.0))]
fn envd(
    endpoint: String,
    access_token: Option<String>,
    user: Option<String>,
    sandbox_id: Option<String>,
    request_timeout: f64,
) -> PyResult<PyEnvd> {
    let mut config = EnvdConfig::new(endpoint);
    config.access_token = access_token.map(Secret::new);
    config.user = user;
    config.sandbox_id = sandbox_id;
    config.request_timeout = timeout(Some(request_timeout))?.expect("positive timeout");
    Envd::new(config)
        .map(|inner| PyEnvd { inner })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (image, *, command=None, env=None, working_dir=None, host_config_json=None))]
fn docker(
    image: String,
    command: Option<Vec<String>>,
    env: Option<BTreeMap<String, String>>,
    working_dir: Option<String>,
    host_config_json: Option<String>,
) -> PyResult<PyDocker> {
    let host_config = host_config_json
        .map(|value| serde_json::from_str(&value))
        .transpose()
        .map_err(|_| {
            exception(Error::invalid(
                "docker",
                "configure",
                "invalid Docker HostConfig JSON",
            ))
        })?;
    let config = DockerConfig {
        image,
        command: command.unwrap_or_default(),
        env: env.unwrap_or_default(),
        working_dir,
        host_config,
    };
    DockerLifecycle::connect_defaults(config)
        .map(|inner| PyDocker { inner })
        .map_err(exception)
}

#[pyfunction]
fn docker_connection(container: String) -> PyResult<PyConnection> {
    DockerConnection::connect_defaults(container)
        .map(|inner| PyConnection {
            inner: Connection::new(inner),
        })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (api_key, template="base", *, api_endpoint="https://api.e2b.app", sandbox_timeout=300, auto_pause=false, env=None, metadata=None, envd_endpoint=None, sandbox_domain="e2b.app", request_timeout=30.0))]
#[allow(
    clippy::too_many_arguments,
    reason = "typed keyword-only E2B configuration"
)]
fn e2b(
    api_key: String,
    template: &str,
    api_endpoint: &str,
    sandbox_timeout: u32,
    auto_pause: bool,
    env: Option<BTreeMap<String, String>>,
    metadata: Option<BTreeMap<String, String>>,
    envd_endpoint: Option<String>,
    sandbox_domain: &str,
    request_timeout: f64,
) -> PyResult<PyE2b> {
    let mut config = E2bConfig::new(Secret::new(api_key), template);
    config.api_endpoint = api_endpoint.into();
    config.sandbox_timeout = sandbox_timeout;
    config.auto_pause = auto_pause;
    config.env = env.unwrap_or_default();
    config.metadata = metadata.unwrap_or_default();
    config.envd_endpoint = envd_endpoint;
    config.sandbox_domain = sandbox_domain.into();
    config.request_timeout = timeout(Some(request_timeout))?.expect("positive timeout");
    E2bLifecycle::new(config)
        .map(|inner| PyE2b { inner })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (namespace, pod_template_json, *, context=None, kubeconfig=None, program="kubectl", service=true, volume_claim_templates_json=None))]
fn agent_sandbox(
    namespace: String,
    pod_template_json: &str,
    context: Option<String>,
    kubeconfig: Option<String>,
    program: &str,
    service: bool,
    volume_claim_templates_json: Option<&str>,
) -> PyResult<PyLifecycle> {
    let template = serde_json::from_str(pod_template_json).map_err(|_| {
        exception(Error::invalid(
            "agent-sandbox",
            "configure",
            "invalid PodTemplate JSON",
        ))
    })?;
    let mut config = AgentSandboxConfig::new(namespace, template);
    config.context = context;
    config.kubeconfig = kubeconfig.map(Into::into);
    config.program = program.into();
    config.service = service;
    if let Some(value) = volume_claim_templates_json {
        config.volume_claim_templates = serde_json::from_str(value).map_err(|_| {
            exception(Error::invalid(
                "agent-sandbox",
                "configure",
                "invalid PVC templates JSON",
            ))
        })?;
    }
    AgentSandboxLifecycle::new(config)
        .map(|inner| PyLifecycle {
            inner: Lifecycle::new(inner),
        })
        .map_err(exception)
}

fn grpc_config(
    endpoint: String,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<GrpcConfig> {
    let mut config = GrpcConfig::new(endpoint);
    config.bearer_token = bearer_token.map(Secret::new);
    config.ca_certificate = ca_certificate.map(Secret::new);
    config.client_identity = match (client_certificate, client_key) {
        (Some(certificate), Some(key)) => Some((Secret::new(certificate), Secret::new(key))),
        (None, None) => None,
        _ => {
            return Err(exception(Error::invalid(
                "python",
                "configure",
                "client_certificate and client_key must be provided together",
            )));
        }
    };
    Ok(config)
}

#[pyfunction]
#[pyo3(signature = (endpoint, *, bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
fn sandboxd(
    py: Python<'_>,
    endpoint: String,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<PyConnection> {
    let config = grpc_config(
        endpoint,
        bearer_token,
        ca_certificate,
        client_certificate,
        client_key,
    )?;
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(Sandboxd::connect(config)))
        .map(|inner| PyConnection {
            inner: Connection::new(inner),
        })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (endpoint, *, workspace="default", workload_template=None, spec_protobuf=None, bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
#[allow(
    clippy::too_many_arguments,
    reason = "Python factory exposes typed keyword-only provider and TLS settings"
)]
fn openshell(
    py: Python<'_>,
    endpoint: String,
    workspace: &str,
    workload_template: Option<String>,
    spec_protobuf: Option<&[u8]>,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<PyOpenShell> {
    let spec = spec_protobuf
        .map(openshell_protocol::v1::SandboxSpec::decode)
        .transpose()
        .map_err(|_| {
            exception(Error::invalid(
                "openshell",
                "configure",
                "invalid OpenShell SandboxSpec protobuf",
            ))
        })?;
    let create = OpenShellCreate {
        spec,
        workload_template,
    };
    let workspace = workspace.to_string();
    let config = grpc_config(
        endpoint,
        bearer_token,
        ca_certificate,
        client_certificate,
        client_key,
    )?;
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(OpenShellLifecycle::connect(config, workspace, create)))
        .map(|inner| PyOpenShell { inner })
        .map_err(exception)
}

#[pyfunction]
#[pyo3(signature = (endpoint, sandbox, *, workspace="default", bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
#[allow(
    clippy::too_many_arguments,
    reason = "Python factory exposes typed keyword-only connection and TLS settings"
)]
fn openshell_connection(
    py: Python<'_>,
    endpoint: String,
    sandbox: String,
    workspace: &str,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<PyConnection> {
    let workspace = workspace.to_string();
    let config = grpc_config(
        endpoint,
        bearer_token,
        ca_certificate,
        client_certificate,
        client_key,
    )?;
    let runtime = runtime()?;
    py.detach(move || runtime.block_on(OpenShellConnection::connect(config, workspace, sandbox)))
        .map(|inner| PyConnection {
            inner: Connection::new(inner),
        })
        .map_err(exception)
}

#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    aio::register(module)?;
    resources::register(module)?;
    module.add_class::<PyResourceRef>()?;
    module.add_class::<PyProcessRef>()?;
    module.add_class::<PyExitStatus>()?;
    module.add_class::<PyProcessOutcome>()?;
    module.add_class::<PyExecResult>()?;
    module.add_class::<PyResourceInfo>()?;
    module.add_class::<PyProcessInfo>()?;
    module.add_class::<PyConnection>()?;
    module.add_class::<PyLifecycle>()?;
    module.add_class::<PyProcessManager>()?;
    module.add_class::<PyProcessHandle>()?;
    module.add_class::<PyProcessEvent>()?;
    module.add_class::<PyEnvd>()?;
    module.add_class::<PyDocker>()?;
    module.add_class::<PyE2b>()?;
    module.add_class::<PyOpenShell>()?;
    module.add_function(wrap_pyfunction!(local, module)?)?;
    module.add_function(wrap_pyfunction!(ssh, module)?)?;
    module.add_function(wrap_pyfunction!(docker, module)?)?;
    module.add_function(wrap_pyfunction!(docker_connection, module)?)?;
    module.add_function(wrap_pyfunction!(pueue, module)?)?;
    module.add_function(wrap_pyfunction!(envd, module)?)?;
    module.add_function(wrap_pyfunction!(e2b, module)?)?;
    module.add_function(wrap_pyfunction!(agent_sandbox, module)?)?;
    module.add_function(wrap_pyfunction!(sandboxd, module)?)?;
    module.add_function(wrap_pyfunction!(openshell, module)?)?;
    module.add_function(wrap_pyfunction!(openshell_connection, module)?)?;
    module.add("SdkError", py.get_type::<SdkError>())?;
    module.add(
        "InvalidArgumentError",
        py.get_type::<InvalidArgumentError>(),
    )?;
    module.add("UnsupportedError", py.get_type::<UnsupportedError>())?;
    module.add("NotFoundError", py.get_type::<NotFoundError>())?;
    module.add("ConflictError", py.get_type::<ConflictError>())?;
    module.add("PermissionError", py.get_type::<PermissionError>())?;
    module.add("TimeoutError", py.get_type::<TimeoutError>())?;
    module.add("TransportError", py.get_type::<TransportError>())?;
    module.add("BackendError", py.get_type::<BackendError>())?;
    Ok(())
}
