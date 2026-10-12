"""matlab_runtime.remote from a real Jupyter kernel, next to matplotlib."""
import os
import os.path as op
import queue
import sys
import textwrap
import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("matplotlib")
pytest.importorskip("ipykernel")
jupyter_client = pytest.importorskip("jupyter_client")

from matlab_runtime.remote import protocol  # noqa: E402

ROOT = op.dirname(op.dirname(op.abspath(__file__)))


def pid_alive(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            return "\nState:\tZ" not in f.read()
    except FileNotFoundError:
        return False


class Kernel:

    def __init__(self, km, kc):
        self.km, self.kc = km, kc

    def execute(self, code, timeout=60, wait_for=None):
        """Run code; return (status, stdout, stderr, error name)."""
        msg_id = self.kc.execute(textwrap.dedent(code))
        return self.collect(msg_id, timeout, wait_for)

    def collect(self, msg_id, timeout=60, wait_for=None):
        out, err, ename, status = "", "", None, None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self.kc.get_iopub_msg(timeout=0.5)
            except queue.Empty:
                continue
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            kind, content = msg["msg_type"], msg["content"]
            if kind == "stream":
                if content["name"] == "stdout":
                    out += content["text"]
                else:
                    err += content["text"]
            elif kind == "error":
                ename = content["ename"]
            elif kind == "status" and content["execution_state"] == "idle":
                status = "idle"
                # Output forwarded from the worker may arrive slightly late
                if wait_for is None or wait_for in out + err:
                    break
                deadline = min(deadline, time.monotonic() + 5)
        return status, out, err, ename


@pytest.fixture
def kernel():
    env = dict(os.environ)
    env[protocol.ENV_BACKEND] = "tests._fake_sdk.backend:FakeBackend"
    env["PYTHONPATH"] = os.pathsep.join(
        [ROOT] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    km = jupyter_client.KernelManager(kernel_name="python3")
    km.kernel_cmd = [
        sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"
    ]
    km.start_kernel(env=env, cwd=ROOT)
    kc = km.client()
    kc.start_channels()
    try:
        kc.wait_for_ready(timeout=60)
        yield Kernel(km, kc)
    finally:
        kc.stop_channels()
        if km.is_alive():
            km.shutdown_kernel(now=True)


def test_jupyter_with_matplotlib(kernel):
    status, out, err, ename = kernel.execute("""
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
        from matlab_runtime import remote

        plt.plot([1, 2, 3])
        plt.savefig(io.BytesIO(), format="png")

        remote.init()
        pkg = remote.import_deployed("fake_package")
        x = pkg.ident(np.eye(3))
        print("RESULT", pkg.test_runtime(), pkg.add(1, 2), x.sum())
        plt.imshow(x)
        plt.savefig(io.BytesIO(), format="png")
        print("WORKER", remote._SESSION.proc.pid)
        pkg.echo_streams()
    """, wait_for="printed by worker")
    assert ename is None, err
    assert "RESULT success 3 3.0" in out
    assert "printed by worker" in out
    worker_pid = int(out.split("WORKER")[1].split()[0])

    # Interrupting the kernel interrupts the wait, not the worker
    msg_id = kernel.kc.execute("pkg.sleep(4)")
    time.sleep(1.5)
    tic = time.monotonic()
    kernel.km.interrupt_kernel()
    status, out, err, ename = kernel.collect(msg_id)
    assert ename == "KeyboardInterrupt", (out, err)
    assert time.monotonic() - tic < 1.5
    assert pid_alive(worker_pid)
    # The next call waits for the worker, and gets its own reply
    status, out, err, ename = kernel.execute("print('NEXT', pkg.add(2, 3))")
    assert "NEXT 5" in out, (out, err, ename)

    # The worker exits with the kernel
    kernel.kc.stop_channels()
    kernel.km.shutdown_kernel(now=False)
    deadline = time.monotonic() + 20
    while pid_alive(worker_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(worker_pid)
