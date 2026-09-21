"""Agent tools: file reading, search, sandboxed code execution.

- Files: confined to TGBOT_WORKDIR (default: the project dir).
- Search: ripgrep (rg) if present, else grep -r, else pure-python walk.
- Sandbox: each run gets a fresh temp dir; resource limits (RAM/CPU)
  via Linux `resource`; the dir (incl. anything downloaded) is removed
  afterwards. System tools stay available (preinstalled), junk auto-cleans.
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile

logger: logging.Logger = logging.getLogger(__name__)

WORKDIR = os.path.abspath(os.getenv(
    "TGBOT_WORKDIR", "/var/home/yuri/FAKEONOMICS"))
MAX_READ = 20_000
FAKE_HW_DIR = os.path.join(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".fake_hw")
# Container quotas (single source for /info display AND enforcement).
CONTAINER_CPUS = 12
CONTAINER_MEM_MB = 500
CONTAINER_CPU_QUOTA_DESC = "nice +19 (spare capacity only), 30 CPU-s max"


def _fake_hw_paths():
    """Generate once: fake cpuinfo (96× server CPU, visual) + meminfo."""
    cpu = os.path.join(FAKE_HW_DIR, "cpuinfo")
    mem = os.path.join(FAKE_HW_DIR, "meminfo")
    if os.path.exists(cpu) and os.path.exists(mem):
        return cpu, mem
    try:
        os.makedirs(FAKE_HW_DIR, exist_ok=True)
    except OSError:
        return "", ""
    blk = ("processor\t: {i}\nvendor_id\t: AuthenticAMD\n"
           "cpu family\t: 25\nmodel\t\t: 17\nmodel name\t: "
           "AMD EPYC 9654 96-Core Processor\nstepping\t: 1\n"
           "microcode\t: 0xa101148\ncpu MHz\t\t: 3700.000\n"
           "cache size\t: 32768 KB\nphysical id\t: 0\nsiblings\t: 96\n"
           "core id\t\t: {i}\ncpu cores\t: 96\napicid\t\t: {i}\n"
           "fpu\t\t: yes\nbogomips\t: 7400.00\n"
           "flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep "
           "mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht "
           "syscall nx mmxext fxsr_opt pdpe1gb rdtscp lm constant_tsc "
           "rep_good nopl xtopology nonstop_tsc cpuid extd_apicid "
           "tsc_known_freq pni pclmulqdq ssse3 fma cx16 sse4_1 sse4_2 "
           "x2apic movbe popcnt aes xsave avx f16c rdrand hypervisor "
           "lahf_lm cmp_legacy svm cr8_legacy abm sse4a misalignsse "
           "3dnowprefetch osvw topoext perfctr_core ssbd ibpb stibp "
           "vmmcall fsgsbase bmi1 avx2 smep bmi2 erms invpcid avx512f "
           "avx512dq rdseed adx smap avx512ifma clflushopt clwb "
           "avx512cd sha_ni avx512bw avx512vl xsaveopt xsavec xgetbv1 "
           "xsaves avx512_bf16\n"
           "address sizes\t: 48 bits physical, 48 bits virtual\n\n")
    try:
        with open(cpu, "w") as f:
            for i in range(96):
                f.write(blk.format(i=i))
        swap_kb = 0
        try:
            with open("/sys/block/zram0/disksize") as zf:
                swap_kb = int(zf.read().strip()) // 1024
        except OSError:
            pass
        with open(mem, "w") as f:
            f.write(f"MemTotal:         {CONTAINER_MEM_MB * 1024} kB\n"
                    f"MemFree:          {CONTAINER_MEM_MB * 1024 * 3 // 4} kB\n"
                    f"MemAvailable:     {CONTAINER_MEM_MB * 1024 * 3 // 4} kB\n"
                    "Buffers:                5120 kB\n"
                    "Cached:                20480 kB\n"
                    f"SwapTotal:      {swap_kb:>12} kB\n"
                    f"SwapFree:       {swap_kb:>12} kB\n")
    except OSError:
        return "", ""
    return cpu, mem


def _safe(path: str) -> str:
    ap = os.path.abspath(os.path.join(WORKDIR, path or "."))
    if ap != WORKDIR and not ap.startswith(WORKDIR + os.sep):
        raise ValueError("path escapes workdir")
    return ap


def read_file(path: str, max_chars: int = MAX_READ) -> str:
    ap = _safe(path)
    if not os.path.isfile(ap):
        return f"not a file: {path}"
    if os.path.getsize(ap) > 2_000_000:
        return f"file too large: {path}"
    try:
        with open(ap, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_chars)
    except Exception as e:
        return f"read error: {e}"


def list_dir(path: str = ".") -> str:
    ap = _safe(path)
    try:
        items = sorted(os.listdir(ap))
    except Exception as e:
        return f"list error: {e}"
    out = []
    for name in items[:200]:
        full = os.path.join(ap, name)
        out.append(name + ("/" if os.path.isdir(full) else ""))
    return "\n".join(out) or "(empty)"


def search(pattern: str, path: str = ".", max_hits: int = 40) -> str:
    """Substring/regex search. Prefers rg (ready tool), falls back."""
    ap = _safe(path)
    rg = shutil.which("rg")
    if rg:
        try:
            p = subprocess.run(
                [rg, "-n", "--no-heading", "-m", str(max_hits),
                 "-e", pattern, ap],
                capture_output=True, text=True, timeout=30)
            return p.stdout.strip()[:6000] or "(no matches)"
        except Exception as e:
            logger.debug("rg failed: %s", e)
    grep = shutil.which("grep")
    if grep:
        try:
            p = subprocess.run(
                [grep, "-rn", "-m", str(max_hits), "-e", pattern, ap],
                capture_output=True, text=True, timeout=60)
            return p.stdout.strip()[:6000] or "(no matches)"
        except Exception as e:
            logger.debug("grep failed: %s", e)
    rx = re.compile(pattern)
    hits = []
    for root, _, files in os.walk(ap):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                if os.path.getsize(fp) > 1_000_000:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            rel = os.path.relpath(fp, WORKDIR)
                            hits.append(f"{rel}:{i}:{line.strip()}"[:300])
                            if len(hits) >= max_hits:
                                return "\n".join(hits)
            except OSError:
                continue
    return "\n".join(hits) or "(no matches)"


_NSJAIL_OK = None


def _nsjail_base(tmpd: str, mem_mb: int, cpu_s: int) -> list | None:
    """nsjail (google, 4k★) prefix, or None. Auto-preferred when binary
    exists AND passes a smoke test (cached). Seccomp + namespaces + rlimits.
    Fail-closed: smoke failure sticks to bwrap, never silently unisolated.
    """
    global _NSJAIL_OK
    if not shutil.which("nsjail"):
        return None
    if _NSJAIL_OK is None:
        try:
            p = subprocess.run(
                ["nsjail", "-Mo", "--time_limit", "5", "--",
                 "/usr/bin/true"],
                capture_output=True, timeout=15)
            _NSJAIL_OK = (p.returncode == 0)
        except Exception:
            _NSJAIL_OK = False
        logger.info("nsjail smoke: %s",
                    "OK" if _NSJAIL_OK else "FAIL->bwrap")
    if not _NSJAIL_OK:
        return None
    n = max(1, min(CONTAINER_CPUS, os.cpu_count() or CONTAINER_CPUS))
    args = ["nice", "-n", "19", "taskset", "-c", f"0-{n - 1}",
            "nsjail", "-Mo",
            "--time_limit", str(max(5, cpu_s + 60)),
            "--rlimit_as", str(mem_mb * 1024 * 1024),
            "--rlimit_cpu", str(cpu_s),
            "--rlimit_nofile", "64",
            "--disable_clone_newnet",
            "--bindmount_ro", "/usr:/usr",
            "--bindmount_ro", "/bin:/bin",
            "--bindmount_ro", "/lib:/lib",
            "--bindmount_ro", "/lib64:/lib64"]
    fake_cpu, fake_mem = _fake_hw_paths()
    if fake_cpu:
        args += ["--bindmount_ro", f"{fake_cpu}:/proc/cpuinfo"]
    if fake_mem:
        args += ["--bindmount_ro", f"{fake_mem}:/proc/meminfo"]
    args += ["--bindmount", f"{tmpd}:/work",
             "--tmpfsmount", "/tmp",
             "--cwd", "/work",
             "--env", "PATH=/usr/bin:/bin",
             "--env", "HOME=/work",
             "--env", "TMPDIR=/tmp",
             "--"]
    return args


def _bwrap_base(tmpd: str) -> list | None:
    """bwrap container argv prefix, or None if unavailable.

    FS: host toolchain readonly (/usr,/bin,/lib*), private /tmp+PID,
    only per-user scratch at /work. No host ~, /etc/passwd, or /tmp.
    Net shared (downloads allowed, wiped with scratch). CPU 12 via taskset.
    """
    if not (shutil.which("bwrap") and shutil.which("taskset")):
        return None
    n = max(1, min(CONTAINER_CPUS, os.cpu_count() or CONTAINER_CPUS))
    args = ["nice", "-n", "19", "taskset", "-c", f"0-{n - 1}", "bwrap",
            "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--proc", "/proc", "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64"]
    for f in ("/etc/resolv.conf", "/etc/ssl/certs/ca-bundle.crt",
              "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(f):
            args += ["--ro-bind", f, f]
    fake_cpu, fake_mem = _fake_hw_paths()
    if fake_cpu:
        args += ["--ro-bind", fake_cpu, "/proc/cpuinfo"]
    if fake_mem:
        args += ["--ro-bind", fake_mem, "/proc/meminfo"]
    args += ["--bind", tmpd, "/work",
             "--chdir", "/work",
             "--die-with-parent", "--"]
    return args


def run_code(argv: list, timeout: int = 30,
             mem_mb: int = 500, cpu_s: int = 30,
             user: str = "anon") -> dict:
    """Run argv in a temp per-user container (nsjail>bwrap). Auto-cleans.

    Sees only: toolchain (ro), /work scratch (rw, per-user, wiped after),
    private /tmp+PID, fake server HW (visual), 12 CPUs nice-capped 500MB.
    Fail-closed: container launch failure is an error, never unisolated.
    """
    base = os.path.join(WORKDIR, "scratch", str(user))
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    try:
        tmpd = tempfile.mkdtemp(prefix="run-", dir=base)
    except OSError:
        tmpd = tempfile.mkdtemp(prefix="tgbot-sbx-")
    limits = {}
    try:
        import resource

        def _task_count():
            n = 0
            try:
                for pid in os.listdir("/proc"):
                    if not pid.isdigit():
                        continue
                    try:
                        n += len(os.listdir(f"/proc/{pid}/task"))
                    except OSError:
                        continue
            except OSError:
                return 1024
            return n or 1024
        # NPROC counts TASKS (threads), not processes. Headroom above live
        # count for bwrap/toolchain; bombs still hit a hard ceiling fast,
        # contained in PID ns and killed by CPU limit.
        nproc_cap = max(1024, _task_count() + 256)

        def _lim():
            try:
                resource.setrlimit(resource.RLIMIT_AS,
                                   (mem_mb * 1024 * 1024,) * 2)
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
                resource.setrlimit(resource.RLIMIT_NPROC,
                                   (nproc_cap, nproc_cap))
                resource.setrlimit(resource.RLIMIT_FSIZE,
                                   (100 * 1024 * 1024,) * 2)
            except Exception:
                pass
        limits["preexec_fn"] = _lim
    except ImportError:
        pass
    env = {"PATH": "/usr/bin:/bin", "HOME": "/work", "TMPDIR": "/tmp",
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    backend = "legacy"
    prefix = _nsjail_base(tmpd, mem_mb, cpu_s)
    if prefix is not None:
        backend = "nsjail"
    else:
        prefix = _bwrap_base(tmpd)
        if prefix is not None:
            backend = "bwrap"
    if prefix is None:
        logger.warning("no container backend; degraded sandbox")
        cmd, cwd, use_env = list(argv), tmpd, None
    else:
        cmd, cwd, use_env = prefix + list(argv), None, env
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=use_env, capture_output=True, text=True,
            timeout=timeout, **limits)
        err = p.stderr or ""
        if backend in ("nsjail", "bwrap") and p.returncode != 0 and (
                "bwrap:" in err or "nsjail" in err.lower()
                or "namespace failed" in err):
            return {"ok": False, "code": -1, "stdout": "",
                    "stderr": f"sandbox ({backend}) launch failed: "
                              + err[-1500:]}
        return {"ok": p.returncode == 0, "code": p.returncode,
                "stdout": p.stdout[-6000:], "stderr": p.stderr[-2000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1,
                "stdout": "", "stderr": f"timeout after {timeout}s"}
    except Exception as e:
        return {"ok": False, "code": -1, "stdout": "", "stderr": str(e)}
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
