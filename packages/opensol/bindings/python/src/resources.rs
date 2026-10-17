//! Bound resource wrappers share the core facade. Construction is local; each operation
//! queries the native manager. Only subscriptions own delivery state, never resources.

use super::*;
use opensol_core::{
    DEFAULT_OUTPUT_LIMIT, ManagerInfo, OutputChunk, OutputCursor, OutputInfo, OutputPage, Process,
    ProcessOutput,
};
use pyo3_async_runtimes::tokio::future_into_py;

#[pyclass(name = "ManagerInfo", frozen)]
pub(super) struct PyManagerInfo {
    pub(super) inner: ManagerInfo,
}
#[pymethods]
impl PyManagerInfo {
    #[getter]
    fn backend(&self) -> &str {
        &self.inner.backend
    }
    #[getter]
    fn client_version(&self) -> Option<&str> {
        self.inner.client_version.as_deref()
    }
    #[getter]
    fn server_version(&self) -> Option<&str> {
        self.inner.server_version.as_deref()
    }
}

fn channel_name(channel: OutputChannel) -> &'static str {
    match channel {
        OutputChannel::Stdout => "stdout",
        OutputChannel::Stderr => "stderr",
        OutputChannel::Terminal => "terminal",
        OutputChannel::Combined => "combined",
    }
}

#[pyclass(name = "OutputInfo", frozen)]
struct PyOutputInfo {
    inner: OutputInfo,
}
#[pymethods]
impl PyOutputInfo {
    #[getter]
    fn channels(&self) -> Vec<&'static str> {
        self.inner
            .channels
            .iter()
            .copied()
            .map(channel_name)
            .collect()
    }
    #[getter]
    fn size(&self) -> u64 {
        self.inner.size
    }
    #[getter]
    fn version(&self) -> Option<&str> {
        self.inner.version.as_deref()
    }
}

#[pyclass(name = "OutputCursor", frozen)]
struct PyOutputCursor {
    inner: OutputCursor,
}
#[pymethods]
impl PyOutputCursor {
    #[new]
    fn new(value: String) -> Self {
        Self {
            inner: OutputCursor(value),
        }
    }
    #[getter]
    fn value(&self) -> &str {
        &self.inner.0
    }
    fn __str__(&self) -> &str {
        &self.inner.0
    }
}

#[pyclass(name = "OutputChunk", frozen)]
struct PyOutputChunk {
    inner: OutputChunk,
}
#[pymethods]
impl PyOutputChunk {
    #[getter]
    fn channel(&self) -> &'static str {
        channel_name(self.inner.channel)
    }
    #[getter]
    fn data<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.inner.data)
    }
}

#[pyclass(name = "OutputPage", frozen)]
struct PyOutputPage {
    inner: OutputPage,
}
#[pymethods]
impl PyOutputPage {
    #[getter]
    fn chunks(&self) -> Vec<PyOutputChunk> {
        self.inner
            .chunks
            .iter()
            .cloned()
            .map(|inner| PyOutputChunk { inner })
            .collect()
    }
    #[getter]
    fn next_cursor(&self) -> PyOutputCursor {
        PyOutputCursor {
            inner: self.inner.next_cursor.clone(),
        }
    }
    #[getter]
    fn at_end(&self) -> bool {
        self.inner.at_end
    }
}

fn output_limit(limit: i64) -> PyResult<usize> {
    let limit = usize::try_from(limit).map_err(|_| {
        exception(Error::invalid(
            "python",
            "read_output",
            "limit must be between 1 and 1048576 bytes",
        ))
    })?;
    Ok(limit)
}

#[pyclass(name = "Process", frozen)]
pub(super) struct PyProcess {
    pub(super) inner: Process,
}
#[pymethods]
impl PyProcess {
    #[getter]
    fn r#ref(&self) -> PyProcessRef {
        PyProcessRef {
            inner: self.inner.reference().clone(),
        }
    }
    #[getter]
    fn output(&self) -> PyProcessOutput {
        PyProcessOutput {
            inner: self.inner.output(),
        }
    }
    fn inspect(&self, py: Python<'_>) -> PyResult<PyProcessInfo> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.inspect()))
            .map(|inner| PyProcessInfo { inner })
            .map_err(exception)
    }
    #[pyo3(signature = (*, timeout=None))]
    fn wait(&self, py: Python<'_>, timeout: Option<f64>) -> PyResult<PyProcessOutcome> {
        let timeout = crate::timeout(timeout)?;
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.wait(timeout)))
            .map(|inner| PyProcessOutcome { inner })
            .map_err(exception)
    }
    fn pause(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.pause()))
            .map_err(exception)
    }
    fn start(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.start()))
            .map_err(exception)
    }
    fn restart(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.restart()))
            .map_err(exception)
    }
    fn kill(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.kill()))
            .map_err(exception)
    }
    fn remove(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.remove()))
            .map_err(exception)
    }
    fn signal(&self, py: Python<'_>, signal: i32) -> PyResult<()> {
        let signal = portable_signal(signal, self.inner.reference().backend.as_str())?;
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.signal(signal)))
            .map_err(exception)
    }
    fn send_input(&self, py: Python<'_>, data: &[u8]) -> PyResult<()> {
        let data = data.to_vec();
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.send_input(data)))
            .map_err(exception)
    }
    fn close_stdin(&self, py: Python<'_>) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.close_stdin()))
            .map_err(exception)
    }
    fn resize(&self, py: Python<'_>, cols: u32, rows: u32) -> PyResult<()> {
        let process = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(process.resize(PtySize { cols, rows })))
            .map_err(exception)
    }
}

#[pyclass(name = "ProcessOutput", frozen)]
struct PyProcessOutput {
    inner: ProcessOutput,
}
#[pymethods]
impl PyProcessOutput {
    /// Current rendered terminal text, not raw output, scrollback, or a cursor-based log.
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
        let output = self.inner.clone();
        let runtime = runtime()?;
        let bytes = py
            .detach(move || runtime.block_on(output.snapshot()))
            .map_err(exception)?;
        Ok(PyBytes::new(py, &bytes))
    }
    fn stat(&self, py: Python<'_>) -> PyResult<PyOutputInfo> {
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.stat()))
            .map(|inner| PyOutputInfo { inner })
            .map_err(exception)
    }
    #[pyo3(signature = (*, cursor=None, limit=DEFAULT_OUTPUT_LIMIT as i64))]
    fn read(
        &self,
        py: Python<'_>,
        cursor: Option<PyRef<'_, PyOutputCursor>>,
        limit: i64,
    ) -> PyResult<PyOutputPage> {
        let limit = output_limit(limit)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.read(cursor.as_ref(), limit)))
            .map(|inner| PyOutputPage { inner })
            .map_err(exception)
    }
    fn subscribe(&self, py: Python<'_>) -> PyResult<PyProcessHandle> {
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.subscribe()))
            .map(Into::into)
            .map_err(exception)
    }
}

#[pyclass(name = "AsyncProcess", frozen)]
pub(super) struct AsyncProcess {
    pub(super) inner: Process,
}
#[pymethods]
impl AsyncProcess {
    #[getter]
    fn r#ref(&self) -> PyProcessRef {
        PyProcessRef {
            inner: self.inner.reference().clone(),
        }
    }
    #[getter]
    fn output(&self) -> AsyncProcessOutput {
        AsyncProcessOutput {
            inner: self.inner.output(),
        }
    }
    fn inspect<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move {
            process
                .inspect()
                .await
                .map(|inner| PyProcessInfo { inner })
                .map_err(exception)
        })
    }
    #[pyo3(signature = (*, timeout=None))]
    fn wait<'py>(&self, py: Python<'py>, timeout: Option<f64>) -> PyResult<Bound<'py, PyAny>> {
        let timeout = crate::timeout(timeout)?;
        let process = self.inner.clone();
        future_into_py(py, async move {
            process
                .wait(timeout)
                .await
                .map(|inner| PyProcessOutcome { inner })
                .map_err(exception)
        })
    }
    fn pause<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move { process.pause().await.map_err(exception) })
    }
    fn start<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move { process.start().await.map_err(exception) })
    }
    fn restart<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(
            py,
            async move { process.restart().await.map_err(exception) },
        )
    }
    fn kill<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move { process.kill().await.map_err(exception) })
    }
    fn remove<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move { process.remove().await.map_err(exception) })
    }
    fn signal<'py>(&self, py: Python<'py>, signal: i32) -> PyResult<Bound<'py, PyAny>> {
        let signal = portable_signal(signal, self.inner.reference().backend.as_str())?;
        let process = self.inner.clone();
        future_into_py(py, async move {
            process.signal(signal).await.map_err(exception)
        })
    }
    fn send_input<'py>(&self, py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
        let data = data.to_vec();
        let process = self.inner.clone();
        future_into_py(py, async move {
            process.send_input(data).await.map_err(exception)
        })
    }
    fn close_stdin<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(
            py,
            async move { process.close_stdin().await.map_err(exception) },
        )
    }
    fn resize<'py>(&self, py: Python<'py>, cols: u32, rows: u32) -> PyResult<Bound<'py, PyAny>> {
        let process = self.inner.clone();
        future_into_py(py, async move {
            process
                .resize(PtySize { cols, rows })
                .await
                .map_err(exception)
        })
    }
}

#[pyclass(name = "AsyncProcessOutput", frozen)]
struct AsyncProcessOutput {
    inner: ProcessOutput,
}
#[pymethods]
impl AsyncProcessOutput {
    fn snapshot<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let output = self.inner.clone();
        future_into_py(py, async move {
            let bytes = output.snapshot().await.map_err(exception)?;
            Ok(Python::attach(|py| PyBytes::new(py, &bytes).unbind()))
        })
    }
    fn stat<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let output = self.inner.clone();
        future_into_py(py, async move {
            output
                .stat()
                .await
                .map(|inner| PyOutputInfo { inner })
                .map_err(exception)
        })
    }
    #[pyo3(signature = (*, cursor=None, limit=DEFAULT_OUTPUT_LIMIT as i64))]
    fn read<'py>(
        &self,
        py: Python<'py>,
        cursor: Option<PyRef<'_, PyOutputCursor>>,
        limit: i64,
    ) -> PyResult<Bound<'py, PyAny>> {
        let limit = output_limit(limit)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let output = self.inner.clone();
        future_into_py(py, async move {
            output
                .read(cursor.as_ref(), limit)
                .await
                .map(|inner| PyOutputPage { inner })
                .map_err(exception)
        })
    }
    fn subscribe<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let output = self.inner.clone();
        future_into_py(py, async move {
            output
                .subscribe()
                .await
                .map(aio::AsyncProcessHandle::from)
                .map_err(exception)
        })
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyManagerInfo>()?;
    module.add_class::<PyOutputInfo>()?;
    module.add_class::<PyOutputCursor>()?;
    module.add_class::<PyOutputChunk>()?;
    module.add_class::<PyOutputPage>()?;
    module.add_class::<PyProcess>()?;
    module.add_class::<PyProcessOutput>()?;
    module.add_class::<AsyncProcess>()?;
    module.add_class::<AsyncProcessOutput>()?;
    Ok(())
}
