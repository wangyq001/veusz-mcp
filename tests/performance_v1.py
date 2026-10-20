"""10,000-point native rendering and clean GUI restart in a disposable session."""
import ctypes
from ctypes import wintypes
import json
import math
import os
from pathlib import Path
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge_cli import launch, process_running
from veusz_bridge.client import Client, RemoteError


def memory(pid):
    class Counters(ctypes.Structure):
        _fields_ = [("cb",wintypes.DWORD),("PageFaultCount",wintypes.DWORD)] + [
            (n,ctypes.c_size_t) for n in ("PeakWorkingSetSize","WorkingSetSize","QuotaPeakPagedPoolUsage",
            "QuotaPagedPoolUsage","QuotaPeakNonPagedPoolUsage","QuotaNonPagedPoolUsage",
            "PagefileUsage","PeakPagefileUsage","PrivateUsage")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    handle = kernel.OpenProcess(0x410,False,pid)
    if not handle:
        return None
    try:
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        psapi = ctypes.WinDLL("psapi")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.c_void_p,wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),counters.cb):
            return None
        return {"working_set_mib":round(counters.WorkingSetSize/1048576,2),
                "private_mib":round(counters.PrivateUsage/1048576,2)}
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)


def main():
    path = ROOT / "runtime/regression-v1/session.json"
    client = Client(path)
    output = ROOT / "output/v0.1/regression"
    source = output / "synthetic_10000.csv"
    with source.open("w",encoding="utf-8",newline="\n") as stream:
        stream.write("x,y\n")
        for i in range(10000):
            stream.write("%.6f,%.8f\n" % (i*.001,math.sin(i*.001)))
    client.call("import_data",{"path":str(source),"columns":{"perf_x":"x","perf_y":"y"}},state=client.state())
    client.call("plot",{"page_name":"benchmark_10000", "series":[{"x":"perf_x","y":"perf_y","name":"line","label":"Synthetic benchmark"}]},state=client.state())
    before = memory(client.state()["pid"])
    latencies = []
    for i in range(100):
        state = client.state()
        start = time.perf_counter()
        state = client.call("set",{"changes":[{"path":"/benchmark_10000/graph/line/PlotLine/width","value":"1pt" if i%2 else "2pt"}]},state=state)
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            rendered=client.state().get("rendered")
            if rendered and rendered["revision"]==state["revision"]:
                latencies.append((time.perf_counter()-start)*1000)
                break
            time.sleep(.01)
        else:
            raise AssertionError("Rendering timeout")
    after = memory(client.state()["pid"])
    client.call("snapshot",{"path":"output/v0.1/regression/benchmark_window.png"},state=client.state())
    size=struct.unpack(">II",(output/"benchmark_window.png").read_bytes()[16:24])
    try:
        client.call("close",state=client.state())
    except RemoteError as exc:
        assert exc.code=="UNSAVED_CHANGES"
    else:
        raise AssertionError("Closed unsaved benchmark")
    client.call("save",{"path":"output/v0.1/regression/performance.vsz","overwrite":True},state=client.state())
    old=client.state()
    client.call("close",state=old)
    deadline=time.monotonic()+8
    while process_running(old["pid"]) and time.monotonic()<deadline:
        time.sleep(.1)
    assert not process_running(old["pid"]),"Clean GUI exit did not finish"
    os.environ["VEUSZ_BRIDGE_TEST_MODE"]="1"
    started=launch(session_file=path)
    assert not started["reused"]
    client=Client(path)
    client.call("open",{"path":"output/v0.1/regression/performance.vsz"},state=client.state())
    reopened=client.state()
    assert reopened["data_summary"]["perf_x"]["length"]==10000
    assert launch(session_file=path)["reused"]
    client.call("close",state=client.state())
    report={"points":10000,"trials":100,"render_p95_ms":round(sorted(latencies)[94],2),
            "render_max_ms":round(max(latencies),2),"window_pixels":size,
            "memory_before":before,"memory_after":after,
            "unsaved_close_rejected":True,"clean_gui_restart_and_reopen":True,
            "repeated_start_reused_window":True}
    (output/"performance.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))


if __name__=="__main__":
    main()
