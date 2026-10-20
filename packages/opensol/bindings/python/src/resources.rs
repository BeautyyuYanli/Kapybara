//! Bound task and representation wrappers share the core facade. Properties and capabilities
//! are local; I/O delegates to native access. Only subscriptions own delivery state.

use super::*;
use opensol_core::{
    DEFAULT_OUTPUT_LIMIT, DEFAULT_TERMINAL_LINES, ManagerInfo, Output, OutputChunk, OutputCursor,
    OutputPage, Process, ReadDirection, Terminal, TerminalCursor, TerminalPage, TerminalScreen,
};
use pyo3::types::{PyBool, PyInt};
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

#[pyclass(name = "TerminalCursor", frozen)]
struct PyTerminalCursor {
    inner: TerminalCursor,
}
#[pymethods]
impl PyTerminalCursor {
    #[new]
    fn new(value: String) -> Self {
        Self {
            inner: TerminalCursor(value),
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
    fn before(&self) -> Option<PyOutputCursor> {
        self.inner
            .before
            .clone()
            .map(|inner| PyOutputCursor { inner })
    }
    #[getter]
    fn after(&self) -> Option<PyOutputCursor> {
        self.inner
            .after
            .clone()
            .map(|inner| PyOutputCursor { inner })
    }
    #[getter]
    fn at_head(&self) -> Option<bool> {
        self.inner.at_head
    }
    #[getter]
    fn at_tail(&self) -> bool {
        self.inner.at_tail
    }
}

#[pyclass(name = "TerminalPage", frozen)]
struct PyTerminalPage {
    inner: TerminalPage,
}
#[pymethods]
impl PyTerminalPage {
    #[getter]
    fn lines(&self) -> Vec<String> {
        self.inner.lines.clone()
    }
    #[getter]
    fn before(&self) -> Option<PyTerminalCursor> {
        self.inner
            .before
            .clone()
            .map(|inner| PyTerminalCursor { inner })
    }
    #[getter]
    fn after(&self) -> Option<PyTerminalCursor> {
        self.inner
            .after
            .clone()
            .map(|inner| PyTerminalCursor { inner })
    }
    #[getter]
    fn at_head(&self) -> Option<bool> {
        self.inner.at_head
    }
    #[getter]
    fn at_tail(&self) -> bool {
        self.inner.at_tail
    }
}

#[pyclass(name = "TerminalScreen", frozen)]
struct PyTerminalScreen {
    inner: TerminalScreen,
}
#[pymethods]
impl PyTerminalScreen {
    #[getter]
    fn cols(&self) -> u32 {
        self.inner.cols
    }
    #[getter]
    fn rows(&self) -> u32 {
        self.inner.rows
    }
    #[getter]
    fn lines(&self) -> Vec<String> {
        self.inner.lines.clone()
    }
}

fn bounded_integer(value: &Bound<'_, PyAny>, max: usize, operation: &str) -> PyResult<usize> {
    if value.is_instance_of::<PyInt>()
        && !value.is_instance_of::<PyBool>()
        && let Ok(size) = value.extract::<usize>()
        && (1..=max).contains(&size)
    {
        return Ok(size);
    }
    Err(exception(Error::invalid(
        "python",
        operation,
        "size must be an integer within the supported range",
    )))
}
fn output_size(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    bounded_integer(value, opensol_core::MAX_OUTPUT_LIMIT, "read_output")
}
fn terminal_lines(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    bounded_integer(value, opensol_core::MAX_TERMINAL_LINES, "read_terminal")
}
fn direction(value: &str) -> PyResult<ReadDirection> {
    match value {
        "forward" => Ok(ReadDirection::Forward),
        "backward" => Ok(ReadDirection::Backward),
        _ => Err(exception(Error::invalid(
            "python",
            "read",
            "direction must be forward or backward",
        ))),
    }
}

#[pyclass(name = "Output", frozen)]
struct PyOutput {
    inner: Output,
}
#[pymethods]
impl PyOutput {
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    #[pyo3(signature = (*, cursor=None, direction="forward", max_bytes=DEFAULT_OUTPUT_LIMIT))]
    fn read(
        &self,
        py: Python<'_>,
        cursor: Option<PyRef<'_, PyOutputCursor>>,
        direction: &str,
        #[pyo3(from_py_with = output_size)] max_bytes: usize,
    ) -> PyResult<PyOutputPage> {
        let direction = self::direction(direction)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.read(cursor.as_ref(), direction, max_bytes)))
            .map(|inner| PyOutputPage { inner })
            .map_err(exception)
    }
    #[pyo3(signature = (*, max_bytes=DEFAULT_OUTPUT_LIMIT))]
    fn tail(
        &self,
        py: Python<'_>,
        #[pyo3(from_py_with = output_size)] max_bytes: usize,
    ) -> PyResult<PyOutputPage> {
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.tail(max_bytes)))
            .map(|inner| PyOutputPage { inner })
            .map_err(exception)
    }
    fn subscribe(&self, py: Python<'_>) -> PyResult<PyOutputSubscription> {
        let output = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(output.subscribe()))
            .map(Into::into)
            .map_err(exception)
    }
}

#[pyclass(name = "Terminal", frozen)]
struct PyTerminal {
    inner: Terminal,
}
#[pymethods]
impl PyTerminal {
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    #[pyo3(signature = (*, cursor=None, direction="forward", lines=DEFAULT_TERMINAL_LINES))]
    fn read(
        &self,
        py: Python<'_>,
        cursor: Option<PyRef<'_, PyTerminalCursor>>,
        direction: &str,
        #[pyo3(from_py_with = terminal_lines)] lines: usize,
    ) -> PyResult<PyTerminalPage> {
        let direction = self::direction(direction)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let terminal = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(terminal.read(cursor.as_ref(), direction, lines)))
            .map(|inner| PyTerminalPage { inner })
            .map_err(exception)
    }
    #[pyo3(signature = (*, lines=DEFAULT_TERMINAL_LINES))]
    fn tail(
        &self,
        py: Python<'_>,
        #[pyo3(from_py_with = terminal_lines)] lines: usize,
    ) -> PyResult<PyTerminalPage> {
        let terminal = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(terminal.tail(lines)))
            .map(|inner| PyTerminalPage { inner })
            .map_err(exception)
    }
    fn screen(&self, py: Python<'_>) -> PyResult<PyTerminalScreen> {
        let terminal = self.inner.clone();
        let runtime = runtime()?;
        py.detach(move || runtime.block_on(terminal.screen()))
            .map(|inner| PyTerminalScreen { inner })
            .map_err(exception)
    }
}

#[pyclass(name = "AsyncOutput", frozen)]
struct AsyncOutput {
    inner: Output,
}
#[pymethods]
impl AsyncOutput {
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    #[pyo3(signature = (*, cursor=None, direction="forward", max_bytes=DEFAULT_OUTPUT_LIMIT))]
    fn read<'py>(
        &self,
        py: Python<'py>,
        cursor: Option<PyRef<'_, PyOutputCursor>>,
        direction: &str,
        #[pyo3(from_py_with = output_size)] max_bytes: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let direction = self::direction(direction)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let output = self.inner.clone();
        future_into_py(py, async move {
            output
                .read(cursor.as_ref(), direction, max_bytes)
                .await
                .map(|inner| PyOutputPage { inner })
                .map_err(exception)
        })
    }
    #[pyo3(signature = (*, max_bytes=DEFAULT_OUTPUT_LIMIT))]
    fn tail<'py>(
        &self,
        py: Python<'py>,
        #[pyo3(from_py_with = output_size)] max_bytes: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let output = self.inner.clone();
        future_into_py(py, async move {
            output
                .tail(max_bytes)
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
                .map(aio::AsyncOutputSubscription::from)
                .map_err(exception)
        })
    }
}

#[pyclass(name = "AsyncTerminal", frozen)]
struct AsyncTerminal {
    inner: Terminal,
}
#[pymethods]
impl AsyncTerminal {
    fn capabilities<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        capabilities(py, self.inner.capabilities())
    }
    #[pyo3(signature = (*, cursor=None, direction="forward", lines=DEFAULT_TERMINAL_LINES))]
    fn read<'py>(
        &self,
        py: Python<'py>,
        cursor: Option<PyRef<'_, PyTerminalCursor>>,
        direction: &str,
        #[pyo3(from_py_with = terminal_lines)] lines: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let direction = self::direction(direction)?;
        let cursor = cursor.map(|cursor| cursor.inner.clone());
        let terminal = self.inner.clone();
        future_into_py(py, async move {
            terminal
                .read(cursor.as_ref(), direction, lines)
                .await
                .map(|inner| PyTerminalPage { inner })
                .map_err(exception)
        })
    }
    #[pyo3(signature = (*, lines=DEFAULT_TERMINAL_LINES))]
    fn tail<'py>(
        &self,
        py: Python<'py>,
        #[pyo3(from_py_with = terminal_lines)] lines: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let terminal = self.inner.clone();
        future_into_py(py, async move {
            terminal
                .tail(lines)
                .await
                .map(|inner| PyTerminalPage { inner })
                .map_err(exception)
        })
    }
    fn screen<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        let terminal = self.inner.clone();
        future_into_py(py, async move {
            terminal
                .screen()
                .await
                .map(|inner| PyTerminalScreen { inner })
                .map_err(exception)
        })
    }
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
    #[getter]
    fn output(&self) -> PyOutput {
        PyOutput {
            inner: self.inner.output(),
        }
    }
    #[getter]
    fn terminal(&self) -> PyTerminal {
        PyTerminal {
            inner: self.inner.terminal(),
        }
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
    #[getter]
    fn output(&self) -> AsyncOutput {
        AsyncOutput {
            inner: self.inner.output(),
        }
    }
    #[getter]
    fn terminal(&self) -> AsyncTerminal {
        AsyncTerminal {
            inner: self.inner.terminal(),
        }
    }
}

pub(super) fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PyManagerInfo>()?;
    module.add_class::<PyOutputCursor>()?;
    module.add_class::<PyTerminalCursor>()?;
    module.add_class::<PyOutputChunk>()?;
    module.add_class::<PyOutputPage>()?;
    module.add_class::<PyTerminalPage>()?;
    module.add_class::<PyTerminalScreen>()?;
    module.add_class::<PyOutput>()?;
    module.add_class::<PyTerminal>()?;
    module.add_class::<AsyncOutput>()?;
    module.add_class::<AsyncTerminal>()?;
    module.add_class::<PyProcess>()?;
    module.add_class::<AsyncProcess>()?;
    Ok(())
}
