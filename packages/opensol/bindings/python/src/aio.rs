//! Async clients drive the native core; blocking views clone those same clients.
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
    pub(super) inner: Connection,
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

#[pyclass(name = "AsyncSandboxManager", frozen)]
pub(super) struct AsyncSandboxManager {
    pub(super) inner: SandboxManager,
}

#[pymethods]
impl AsyncSandboxManager {
    fn _blocking(&self) -> PySandboxManager {
        PySandboxManager {
            inner: self.inner.clone(),
        }
    }

    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    fn get(&self, sandbox: PyRef<'_, PySandboxRef>) -> AsyncSandbox {
        AsyncSandbox {
            inner: self.inner.get(sandbox.inner.clone()),
        }
    }
    fn list<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .list()
                .await
                .map(|items| {
                    items
                        .into_iter()
                        .map(|inner| PySandboxInfo { inner })
                        .collect::<Vec<_>>()
                })
                .map_err(exception)
        })
    }
    fn create<'py>(&self, py: Python<'py>, spec: &str) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        let spec = construction::sandbox_spec(spec)?;
        future_into_py(py, async move {
            client
                .create(spec)
                .await
                .map(|inner| AsyncSandbox { inner })
                .map_err(exception)
        })
    }
}

#[pyclass(name = "AsyncSandbox", frozen)]
pub(super) struct AsyncSandbox {
    inner: opensol_core::Sandbox,
}

#[pymethods]
impl AsyncSandbox {
    #[getter]
    fn r#ref(&self) -> PySandboxRef {
        PySandboxRef {
            inner: self.inner.reference().clone(),
        }
    }
    fn inspect<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .inspect()
                .await
                .map(|inner| PySandboxInfo { inner })
                .map_err(exception)
        })
    }
    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move { client.start().await.map_err(exception) })
    }
    fn stop<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move { client.stop().await.map_err(exception) })
    }
    fn delete<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move { client.delete().await.map_err(exception) })
    }
    fn fork<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let client = self.inner.clone();
        future_into_py(py, async move {
            client
                .fork()
                .await
                .map(|inner| AsyncSandbox { inner })
                .map_err(exception)
        })
    }
}

/// The Python wrapper serializes consumers, while this lock also covers a bridge
/// future that has been cancelled but has not yet stopped on the Rust executor.
#[pyclass(name = "AsyncOutputSubscription", frozen)]
pub(super) struct AsyncOutputSubscription {
    backend: String,
    state: Arc<tokio::sync::Mutex<SubscriptionState>>,
    pending: Arc<Mutex<Option<Result<ProcessEvent>>>>,
    abort: AbortHandle,
    closed: Arc<AtomicBool>,
}

impl From<OutputSubscription> for AsyncOutputSubscription {
    fn from(subscription: OutputSubscription) -> Self {
        let (abort, registration) = AbortHandle::new_pair();
        Self {
            backend: subscription.backend().to_owned(),
            state: Arc::new(tokio::sync::Mutex::new(SubscriptionState {
                events: Some(Box::pin(Abortable::new(subscription.events, registration))),
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
impl AsyncOutputSubscription {
    #[pyo3(signature = (*, timeout=None))]
    fn _reserve<'py>(&self, py: Python<'py>, timeout: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let timeout = crate::timeout(timeout)?;
        let state = self.state.clone();
        let pending = self.pending.clone();
        let closed = self.closed.clone();
        let backend = self.backend.clone();
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
            let consume = subscription_event(&mut state, &closed, &backend);
            let event = match timeout {
                Some(timeout) => tokio::time::timeout(timeout, consume)
                    .await
                    .map_err(|_| exception(subscription_timeout(&backend, "output")))?,
                None => consume.await,
            };
            // Shared consumption has already recorded completion or terminal closure.
            // No await separates that native result from reserving its Python delivery;
            // a timer expiry above leaves both the stream and pending slot untouched.
            match event {
                Ok(Some(event)) => {
                    *pending.lock().map_err(|_| delivery_lock_error())? = Some(Ok(event.clone()));
                    Ok(Some(PyProcessEvent { inner: event }))
                }
                Err(error) => {
                    *pending.lock().map_err(|_| delivery_lock_error())? = Some(Err(error.clone()));
                    Err(exception(error))
                }
                Ok(None) => Ok(None),
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
pub(super) struct AsyncProcessManager {
    pub(super) inner: ProcessManager,
}

#[pymethods]
impl AsyncProcessManager {
    fn _blocking(&self) -> PyProcessManager {
        PyProcessManager {
            inner: self.inner.clone(),
        }
    }

    #[getter]
    fn backend(&self) -> &'static str {
        self.inner.backend()
    }

    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }

    fn get(&self, process: PyRef<'_, PyProcessRef>) -> resources::AsyncProcess {
        resources::AsyncProcess {
            inner: self.inner.get(process.inner.clone()),
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
                .map(|process| resources::AsyncProcess { inner: process })
                .map_err(exception)
        })
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<AsyncConnection>()?;
    module.add_class::<AsyncSandboxManager>()?;
    module.add_class::<AsyncSandbox>()?;
    module.add_class::<AsyncProcessManager>()?;
    module.add_class::<AsyncOutputSubscription>()?;
    Ok(())
}
