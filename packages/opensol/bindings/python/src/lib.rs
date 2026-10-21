//! Shared Python parameter conversion, blocking wrappers, and native async wrappers.

mod aio;
mod construction;
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
use opensol_core::{
    Command, Connection, Error, ErrorKind, EventStream, ExecRequest, ExecResult, ExitStatus,
    OutputChannel, OutputSubscription, ProcessEvent, ProcessInfo, ProcessManager, ProcessOutcome,
    ProcessRef, PtySize, Result, SandboxInfo, SandboxManager, SandboxRef, Signal, SpawnRequest,
};
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

#[pyclass(name = "SandboxRef", frozen)]
#[derive(Clone)]
struct PySandboxRef {
    inner: SandboxRef,
}

#[pymethods]
impl PySandboxRef {
    #[new]
    fn new(backend: String, id: String) -> Self {
        Self {
            inner: SandboxRef::new(&backend, id),
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
        format!("SandboxRef({:?}, {:?})", self.inner.backend, self.inner.id)
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

#[pyclass(name = "SandboxInfo", frozen)]
struct PySandboxInfo {
    inner: SandboxInfo,
}

#[pymethods]
impl PySandboxInfo {
    #[getter]
    fn sandbox(&self) -> PySandboxRef {
        PySandboxRef {
            inner: self.inner.sandbox.clone(),
        }
    }
    #[getter]
    fn name(&self) -> Option<&str> {
        self.inner.name.as_deref()
    }
    #[getter]
    fn labels(&self) -> BTreeMap<String, String> {
        self.inner.labels.clone()
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
#[pyclass(name = "SandboxManager", frozen)]
struct PySandboxManager {
    inner: SandboxManager,
}

#[pymethods]
impl PySandboxManager {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    fn get(&self, sandbox: PyRef<'_, PySandboxRef>) -> PySandbox {
        PySandbox {
            inner: self.inner.get(sandbox.inner.clone()),
        }
    }
    fn list(&self, py: Python<'_>) -> PyResult<Vec<PySandboxInfo>> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.list()))
            .map(|items| {
                items
                    .into_iter()
                    .map(|inner| PySandboxInfo { inner })
                    .collect()
            })
            .map_err(exception)
    }
    fn create(&self, py: Python<'_>, spec: &str) -> PyResult<PySandbox> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        let spec = construction::sandbox_spec(spec)?;
        py.detach(move || runtime.block_on(client.create(spec)))
            .map(|inner| PySandbox { inner })
            .map_err(exception)
    }
}

/// Locally bound sandbox; dropping it does not delete the native environment.
#[pyclass(name = "Sandbox", frozen)]
struct PySandbox {
    inner: opensol_core::Sandbox,
}

#[pymethods]
impl PySandbox {
    #[getter]
    fn r#ref(&self) -> PySandboxRef {
        PySandboxRef {
            inner: self.inner.reference().clone(),
        }
    }
    fn inspect(&self, py: Python<'_>) -> PyResult<PySandboxInfo> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.inspect()))
            .map(|inner| PySandboxInfo { inner })
            .map_err(exception)
    }
    fn start(&self, py: Python<'_>) -> PyResult<()> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.start()))
            .map_err(exception)
    }
    fn stop(&self, py: Python<'_>) -> PyResult<()> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.stop()))
            .map_err(exception)
    }
    fn delete(&self, py: Python<'_>) -> PyResult<()> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.delete()))
            .map_err(exception)
    }
    fn fork(&self, py: Python<'_>) -> PyResult<PySandbox> {
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.fork()))
            .map(|inner| PySandbox { inner })
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

struct SubscriptionState {
    events: Option<EventStream>,
}

/// One output subscription. close releases it without killing the remote process.
#[pyclass(name = "OutputSubscription", frozen)]
struct PyOutputSubscription {
    backend: String,
    state: Arc<tokio::sync::Mutex<SubscriptionState>>,
    abort: AbortHandle,
    closed: Arc<AtomicBool>,
}

impl From<OutputSubscription> for PyOutputSubscription {
    fn from(subscription: OutputSubscription) -> Self {
        let (abort, registration) = AbortHandle::new_pair();
        Self {
            backend: subscription.backend().to_owned(),
            state: Arc::new(tokio::sync::Mutex::new(SubscriptionState {
                events: Some(Box::pin(Abortable::new(subscription.events, registration))),
            })),
            abort,
            closed: Arc::new(AtomicBool::new(false)),
        }
    }
}

/// Call with consumption ownership held. Cancellation while awaiting the native stream
/// leaves it attached; completion is delivered once for this subscription's execution.
/// Callers apply nonterminal read timeouts outside this function, so returned errors
/// are terminal stream failures that aio must reserve until Python acknowledges them.
async fn subscription_event(
    state: &mut SubscriptionState,
    closed: &AtomicBool,
    backend: &str,
) -> Result<Option<ProcessEvent>> {
    let Some(events) = state.events.as_mut() else {
        return Ok(None);
    };
    let event = events.next().await;
    match event {
        Some(Ok(event)) => {
            if matches!(event, ProcessEvent::Completed(_)) {
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
            if !closed.load(Ordering::Acquire) {
                Err(Error::new(
                    ErrorKind::Transport,
                    backend,
                    "output",
                    "subscription ended before process completion",
                ))
            } else {
                Ok(None)
            }
        }
    }
}

fn subscription_timeout(backend: &str, operation: &str) -> Error {
    Error::new(
        ErrorKind::Timeout,
        backend,
        operation,
        "output wait timed out",
    )
}

#[pymethods]
impl PyOutputSubscription {
    #[pyo3(signature = (*, timeout=None))]
    fn next_event(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<Option<PyProcessEvent>> {
        let timeout = crate::timeout(timeout)?;
        let state = self.state.clone();
        let backend = self.backend.clone();
        let closed = self.closed.clone();
        let runtime = runtime()?;
        py.detach(move || {
            runtime.block_on(async {
                let mut state = state.lock().await;
                let consume = subscription_event(&mut state, &closed, &backend);
                match timeout {
                    Some(timeout) => tokio::time::timeout(timeout, consume)
                        .await
                        .map_err(|_| subscription_timeout(&backend, "output"))?,
                    None => consume.await,
                }
            })
        })
        .map(|event| event.map(|inner| PyProcessEvent { inner }))
        .map_err(exception)
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.closed.store(true, Ordering::Release);
        self.abort.abort();
        let state = self.state.clone();
        let runtime = runtime()?;
        py.detach(move || {
            runtime.block_on(async {
                state.lock().await.events = None;
            });
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
    /// Create a bound task, preserving an indispensable Start stream only on envd.
    fn spawn(
        &self,
        py: Python<'_>,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: bool,
        pty: Option<(u32, u32)>,
    ) -> PyResult<resources::PyProcess> {
        let request = SpawnRequest {
            command: command(argv, cwd, env),
            stdin,
            pty: pty.map(|(cols, rows)| PtySize { cols, rows }),
        };
        let client = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(client.spawn(request)))
            .map(|process| resources::PyProcess { inner: process })
            .map_err(exception)
    }
}

#[pymodule]
fn _native(py: Python<'_>, module: &Bound<'_, PyModule>) -> PyResult<()> {
    aio::register(module)?;
    resources::register(module)?;
    module.add_class::<PySandboxRef>()?;
    module.add_class::<PyProcessRef>()?;
    module.add_class::<PyExitStatus>()?;
    module.add_class::<PyProcessOutcome>()?;
    module.add_class::<PyExecResult>()?;
    module.add_class::<PySandboxInfo>()?;
    module.add_class::<PyProcessInfo>()?;
    module.add_class::<PyConnection>()?;
    module.add_class::<PySandboxManager>()?;
    module.add_class::<PySandbox>()?;
    module.add_class::<PyProcessManager>()?;
    module.add_class::<PyOutputSubscription>()?;
    module.add_class::<PyProcessEvent>()?;
    construction::register(module)?;
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
