//! Awaitables share the blocking wrappers' clients, DTOs and conversions.
//!
//! Stream consumption and Python delivery are separate: one pending event remains
//! reserved until the Python coroutine resumes and acknowledges it synchronously.
//! Cancelling the bridge future therefore never discards a consumed event.
//! Terminal stream errors use the same delivery slot so cancellation cannot turn
//! an unexpected end into an apparently clean end of iteration.

use super::*;
use pyo3_async_runtimes::tokio::future_into_py;

#[pyclass(name = "AsyncConnection", frozen)]
pub(super) struct AsyncConnection {
    inner: Arc<dyn Connection>,
}

#[pymethods]
impl AsyncConnection {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }

    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    fn _blocking(&self) -> PyConnection {
        PyConnection {
            inner: self.inner.clone(),
        }
    }

    #[pyo3(signature = (argv, *, cwd=None, env=None, stdin=b"".as_slice(), timeout=None))]
    fn exec<'py>(
        &self,
        py: Python<'py>,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: &[u8],
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request = ExecRequest {
            command: command(argv, cwd, env),
            stdin: stdin.to_vec(),
            timeout: crate::timeout(timeout)?,
        };
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .exec(request)
                .await
                .map(|inner| PyExecResult { inner })
                .map_err(exception)
        })
    }

    #[pyo3(signature = (script, *, cwd=None, env=None, stdin=b"".as_slice(), timeout=None))]
    fn shell<'py>(
        &self,
        py: Python<'py>,
        script: String,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: &[u8],
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        self.exec(py, Command::shell(script).argv, cwd, env, stdin, timeout)
    }
}

#[pyclass(name = "AsyncLifecycle", frozen)]
pub(super) struct AsyncLifecycle {
    inner: Arc<dyn Lifecycle>,
}

#[pymethods]
impl AsyncLifecycle {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }

    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    #[pyo3(signature = (*, name=None, labels=None))]
    fn create<'py>(
        &self,
        py: Python<'py>,
        name: Option<String>,
        labels: Option<BTreeMap<String, String>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .create(CreateOptions {
                    name,
                    labels: labels.unwrap_or_default(),
                })
                .await
                .map(|inner| PyResourceRef { inner })
                .map_err(exception)
        })
    }

    fn inspect<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        future_into_py(py, async move {
            client
                .inspect(&resource)
                .await
                .map(|inner| PyResourceInfo { inner })
                .map_err(exception)
        })
    }

    fn start<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        future_into_py(py, async move {
            client.start(&resource).await.map_err(exception)
        })
    }

    fn stop<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        future_into_py(py, async move {
            client.stop(&resource).await.map_err(exception)
        })
    }

    fn delete<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        future_into_py(py, async move {
            client.delete(&resource).await.map_err(exception)
        })
    }

    fn fork<'py>(
        &self,
        py: Python<'py>,
        resource: PyRef<'_, PyResourceRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let resource = resource.inner.clone();
        future_into_py(py, async move {
            client
                .fork(&resource)
                .await
                .map(|inner| PyResourceRef { inner })
                .map_err(exception)
        })
    }
}

struct StreamState {
    events: Option<EventStream>,
    exit: Option<ExitStatus>,
}

/// The Python wrapper serializes consumers, while this lock also covers a bridge
/// future that has been cancelled but has not yet stopped on the Rust executor.
#[pyclass(name = "AsyncProcessHandle", frozen)]
pub(super) struct AsyncProcessHandle {
    process: ProcessRef,
    state: Arc<tokio::sync::Mutex<StreamState>>,
    pending: Arc<Mutex<Option<Result<ProcessEvent>>>>,
    abort: AbortHandle,
    closed: Arc<AtomicBool>,
}

impl From<ProcessHandle> for AsyncProcessHandle {
    fn from(handle: ProcessHandle) -> Self {
        let (abort, registration) = AbortHandle::new_pair();
        Self {
            process: handle.process,
            state: Arc::new(tokio::sync::Mutex::new(StreamState {
                events: Some(Box::pin(Abortable::new(handle.events, registration))),
                exit: None,
            })),
            pending: Arc::new(Mutex::new(None)),
            abort,
            closed: Arc::new(AtomicBool::new(false)),
        }
    }
}

fn delivery_lock_error() -> PyErr {
    exception(Error::new(
        ErrorKind::Backend,
        "python",
        "output",
        "output delivery lock failed",
    ))
}

#[pymethods]
impl AsyncProcessHandle {
    #[getter]
    fn process(&self) -> PyProcessRef {
        PyProcessRef {
            inner: self.process.clone(),
        }
    }

    #[pyo3(signature = (*, timeout=None))]
    fn _reserve<'py>(&self, py: Python<'py>, timeout: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let timeout = crate::timeout(timeout)?;
        let state = self.state.clone();
        let pending = self.pending.clone();
        let closed = self.closed.clone();
        let backend = self.process.backend.clone();
        future_into_py(py, async move {
            let mut state = state.lock().await;
            if closed.load(Ordering::Acquire) {
                return Ok(None);
            }
            // Building a Python exception may acquire the GIL. Release the
            // delivery mutex first; synchronous acknowledgement holds the GIL.
            let reserved = pending.lock().map_err(|_| delivery_lock_error())?.clone();
            if let Some(event) = reserved {
                return event
                    .map(|inner| Some(PyProcessEvent { inner }))
                    .map_err(exception);
            }
            let Some(events) = state.events.as_mut() else {
                return Ok(None);
            };
            let event = match timeout {
                Some(timeout) => {
                    tokio::time::timeout(timeout, events.next())
                        .await
                        .map_err(|_| {
                            exception(Error::new(
                                ErrorKind::Timeout,
                                &backend,
                                "output",
                                "output wait timed out",
                            ))
                        })?
                }
                None => events.next().await,
            };
            match event {
                Some(Ok(event)) => {
                    if let ProcessEvent::Exit(exit) = event {
                        state.exit = Some(exit);
                        state.events = None;
                    }
                    // No await separates consuming the stream and reserving delivery.
                    *pending.lock().map_err(|_| delivery_lock_error())? = Some(Ok(event.clone()));
                    Ok(Some(PyProcessEvent { inner: event }))
                }
                Some(Err(error)) => {
                    state.events = None;
                    *pending.lock().map_err(|_| delivery_lock_error())? = Some(Err(error.clone()));
                    Err(exception(error))
                }
                None => {
                    state.events = None;
                    if state.exit.is_none() && !closed.load(Ordering::Acquire) {
                        let error = Error::new(
                            ErrorKind::Transport,
                            &backend,
                            "output",
                            "subscription ended before exit status",
                        );
                        *pending.lock().map_err(|_| delivery_lock_error())? =
                            Some(Err(error.clone()));
                        Err(exception(error))
                    } else {
                        Ok(None)
                    }
                }
            }
        })
    }

    /// Called synchronously, without an intervening await, only after Python has
    /// received a reserved event. Cancellation before that point retains it.
    fn _commit(&self) -> PyResult<()> {
        self.pending
            .lock()
            .map_err(|_| delivery_lock_error())?
            .take();
        Ok(())
    }

    fn _exit<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let state = self.state.clone();
        let backend = self.process.backend.clone();
        future_into_py(py, async move {
            state
                .lock()
                .await
                .exit
                .map(|inner| PyExitStatus { inner })
                .ok_or_else(|| {
                    exception(Error::new(
                        ErrorKind::Transport,
                        &backend,
                        "wait",
                        "subscription is disconnected; exit status is unknown",
                    ))
                })
        })
    }

    fn aclose<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        self.closed.store(true, Ordering::Release);
        self.abort.abort();
        let state = self.state.clone();
        let pending = self.pending.clone();
        future_into_py(py, async move {
            state.lock().await.events = None;
            pending.lock().map_err(|_| delivery_lock_error())?.take();
            Ok(())
        })
    }
}

#[pyclass(name = "AsyncProcessManager", frozen)]
struct AsyncProcessManager {
    inner: Arc<dyn ProcessManager>,
}

#[pymethods]
impl AsyncProcessManager {
    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }

    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    fn get(&self, process: PyRef<'_, PyProcessRef>) -> resources::AsyncProcess {
        resources::AsyncProcess {
            inner: opensol_core::Process::new(self.inner.clone(), process.inner.clone()),
        }
    }

    fn probe<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .probe()
                .await
                .map(|inner| resources::PyManagerInfo { inner })
                .map_err(exception)
        })
    }

    fn list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .list()
                .await
                .map(|list| {
                    list.into_iter()
                        .map(|inner| PyProcessInfo { inner })
                        .collect::<Vec<_>>()
                })
                .map_err(exception)
        })
    }

    #[pyo3(signature = (argv, *, cwd=None, env=None, stdin=false, pty=None))]
    fn spawn<'py>(
        &self,
        py: Python<'py>,
        argv: Vec<String>,
        cwd: Option<String>,
        env: Option<BTreeMap<String, String>>,
        stdin: bool,
        pty: Option<(u32, u32)>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let request = SpawnRequest {
            command: command(argv, cwd, env),
            stdin,
            pty: pty.map(|(cols, rows)| PtySize { cols, rows }),
        };
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .spawn(request)
                .await
                .map(AsyncProcessHandle::from)
                .map_err(exception)
        })
    }

    fn status<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .status(&process)
                .await
                .map(|inner| PyProcessInfo { inner })
                .map_err(exception)
        })
    }

    #[pyo3(signature = (process, *, timeout=None))]
    fn wait<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
        timeout: Option<f64>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let timeout = crate::timeout(timeout)?;
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .wait(&process, timeout)
                .await
                .map(|inner| PyExitStatus { inner })
                .map_err(exception)
        })
    }

    fn output<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            let events = client.output(&process).await.map_err(exception)?;
            Ok(AsyncProcessHandle::from(ProcessHandle { process, events }))
        })
    }

    fn reconnect<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .reconnect(&process)
                .await
                .map(AsyncProcessHandle::from)
                .map_err(exception)
        })
    }

    fn send_input<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
        data: &[u8],
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let data = data.to_vec();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client.send_input(&process, data).await.map_err(exception)
        })
    }

    fn close_stdin<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client.close_stdin(&process).await.map_err(exception)
        })
    }

    fn signal<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
        signal: i32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let signal = portable_signal(signal, self.inner.backend())?;
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client.signal(&process, signal).await.map_err(exception)
        })
    }

    fn resize<'py>(
        &self,
        py: Python<'py>,
        process: PyRef<'_, PyProcessRef>,
        cols: u32,
        rows: u32,
    ) -> PyResult<Bound<'py, PyAny>> {
        let process = process.inner.clone();
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .resize(&process, PtySize { cols, rows })
                .await
                .map_err(exception)
        })
    }
}

#[pyfunction]
fn _async_connection(connection: PyRef<'_, PyConnection>) -> AsyncConnection {
    AsyncConnection {
        inner: connection.inner.clone(),
    }
}

#[pyfunction]
fn _async_lifecycle(lifecycle: PyRef<'_, PyLifecycle>) -> AsyncLifecycle {
    AsyncLifecycle {
        inner: lifecycle.inner.clone(),
    }
}

#[pyfunction]
fn _async_processes(manager: PyRef<'_, PyProcessManager>) -> AsyncProcessManager {
    AsyncProcessManager {
        inner: manager.inner.clone(),
    }
}

#[pyfunction]
#[pyo3(signature = (api_key, resource, *, api_endpoint="https://api.e2b.app", envd_endpoint=None))]
fn _aio_e2b_envd<'py>(
    py: Python<'py>,
    api_key: String,
    resource: PyRef<'_, PyResourceRef>,
    api_endpoint: &str,
    envd_endpoint: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let mut config = E2bConfig::new(Secret::new(api_key), "base");
    config.api_endpoint = api_endpoint.into();
    config.envd_endpoint = envd_endpoint;
    let client = E2bLifecycle::new(config).map_err(exception)?;
    let resource = resource.inner.clone();
    future_into_py(py, async move {
        client
            .envd(&resource)
            .await
            .map(|inner| PyEnvd {
                inner: Arc::new(inner),
            })
            .map_err(exception)
    })
}

#[pyfunction]
#[pyo3(signature = (endpoint, *, bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
fn _aio_sandboxd<'py>(
    py: Python<'py>,
    endpoint: String,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let config = grpc_config(
        endpoint,
        bearer_token,
        ca_certificate,
        client_certificate,
        client_key,
    )?;
    future_into_py(py, async move {
        Sandboxd::connect(config)
            .await
            .map(|inner| AsyncConnection {
                inner: Arc::new(inner),
            })
            .map_err(exception)
    })
}

#[pyfunction]
#[pyo3(signature = (endpoint, *, workspace="default", workload_template=None, spec_protobuf=None, bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
#[allow(
    clippy::too_many_arguments,
    reason = "typed keyword-only provider and TLS settings"
)]
fn _aio_openshell<'py>(
    py: Python<'py>,
    endpoint: String,
    workspace: &str,
    workload_template: Option<String>,
    spec_protobuf: Option<&[u8]>,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
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
    future_into_py(py, async move {
        OpenShellLifecycle::connect(config, workspace, create)
            .await
            .map(|inner| AsyncLifecycle {
                inner: Arc::new(inner),
            })
            .map_err(exception)
    })
}

#[pyfunction]
#[pyo3(signature = (endpoint, sandbox, *, workspace="default", bearer_token=None, ca_certificate=None, client_certificate=None, client_key=None))]
#[allow(
    clippy::too_many_arguments,
    reason = "typed keyword-only provider and TLS settings"
)]
fn _aio_openshell_connection<'py>(
    py: Python<'py>,
    endpoint: String,
    sandbox: String,
    workspace: &str,
    bearer_token: Option<String>,
    ca_certificate: Option<String>,
    client_certificate: Option<String>,
    client_key: Option<String>,
) -> PyResult<Bound<'py, PyAny>> {
    let workspace = workspace.to_string();
    let config = grpc_config(
        endpoint,
        bearer_token,
        ca_certificate,
        client_certificate,
        client_key,
    )?;
    future_into_py(py, async move {
        OpenShellConnection::connect(config, workspace, sandbox)
            .await
            .map(|inner| AsyncConnection {
                inner: Arc::new(inner),
            })
            .map_err(exception)
    })
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<AsyncConnection>()?;
    module.add_class::<AsyncLifecycle>()?;
    module.add_class::<AsyncProcessManager>()?;
    module.add_class::<AsyncProcessHandle>()?;
    module.add_function(wrap_pyfunction!(_async_connection, module)?)?;
    module.add_function(wrap_pyfunction!(_async_lifecycle, module)?)?;
    module.add_function(wrap_pyfunction!(_async_processes, module)?)?;
    module.add_function(wrap_pyfunction!(_aio_e2b_envd, module)?)?;
    module.add_function(wrap_pyfunction!(_aio_sandboxd, module)?)?;
    module.add_function(wrap_pyfunction!(_aio_openshell, module)?)?;
    module.add_function(wrap_pyfunction!(_aio_openshell_connection, module)?)?;
    Ok(())
}
