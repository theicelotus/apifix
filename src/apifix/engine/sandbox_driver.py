"""Runs INSIDE sandbox containers, under the target repo's Python (3.8+): keep it 3.8-compatible
and stdlib-only. The host never imports this module; it is copied into the container.

    driver.py install [--constraints]   network on;  sees /manifest and /deps only (and the
                                        pip cache, if one is mounted); ends with one
                                        INSTALL_STATS_MARKER line (downloads vs cache hits)
    driver.py test [--coverage] [--path DIR]... -- <cmd...>
                                        network off; /deps read-only, /work = repo copy;
                                        each DIR (repo-relative) goes on PYTHONPATH
    driver.py smoke <distribution>      network off; /deps read-only, no repo: runs the
                                        migration's /apifix/smoke_check.py with the deps venv

Test phase layout: /deps is read-only, so an overlay venv in /tmp sees the dependencies via a
.pth file that calls site.addsitedir(); the repo is installed into the overlay (editable, no
network, no deps). If that fails, the repo is put on PYTHONPATH instead.
"""

import json
import os
import re
import shutil
import subprocess
import sys

# Container paths. Module-level so tests can point them at temp dirs on the host.
DEPS = "/deps"
MANIFEST = "/manifest"
WORK = "/work"
RESULTS = "/results"
OVERLAY = "/tmp/venv"
MAX_LOG = 8000
NO_PROJECT = "no pyproject.toml or setup.py"
SMOKE_CHECK = "/apifix/smoke_check.py"
SMOKE_MARKER = "APIFIX_SMOKE_RESULT "
INSTALL_STATS_MARKER = "APIFIX_INSTALL_STATS "
# pip's "Downloading x.whl (7.2 MB)" / "Using cached x.whl (7.2 MB)" (older pip: a URL, "7.2MB")
_FILE_LINE = re.compile(
    r"^\s*(Downloading|Using cached) (\S+) \((\d+(?:\.\d+)?) ?(bytes|kB|MB|GB)\)"
)
_UNITS = {"bytes": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3}


def _site_dirs(python):
    code = (
        "import sysconfig, json; "
        "print(json.dumps(sorted({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')})))"
    )
    return json.loads(subprocess.check_output([python, "-c", code]).decode())


def _venv_python(venv):
    return os.path.join(venv, "bin", "python")


def install(use_constraints):
    tmp = os.path.join(DEPS, ".tmp")  # disk-backed build dir: /tmp is a small in-memory tmpfs
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TMPDIR=tmp)
    venv = os.path.join(DEPS, "venv")
    subprocess.check_call([sys.executable, "-m", "venv", venv])
    cmd = [_venv_python(venv), "-m", "pip", "install", "-r", MANIFEST + "/requirements.txt"]
    if use_constraints:
        cmd += ["-c", MANIFEST + "/constraints.txt"]
    print("[apifix] " + " ".join(cmd), file=sys.stderr, flush=True)
    stats = {"downloaded": 0, "downloaded_bytes": 0, "cached": 0, "cached_bytes": 0}
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE)
    for raw in proc.stdout:  # passed through unchanged, counted on the way
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
        count_file_line(raw.decode("utf-8", "replace"), stats)
    rc = proc.wait()
    shutil.rmtree(tmp, ignore_errors=True)
    print(INSTALL_STATS_MARKER + json.dumps(stats), flush=True)
    return rc


def count_file_line(line, stats):
    """Count one distribution file pip downloaded or took from its cache (not .metadata)."""
    m = _FILE_LINE.match(line)
    if not m or m.group(2).endswith(".metadata"):
        return
    key = "downloaded" if m.group(1) == "Downloading" else "cached"
    stats[key] += 1
    stats[key + "_bytes"] += int(float(m.group(3)) * _UNITS[m.group(4)])


def _write_results(data):
    with open(os.path.join(RESULTS, "driver.json"), "w") as f:
        json.dump(data, f)


def test(command, coverage=False, paths=()):
    deps_venv = os.path.join(DEPS, "venv")
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", OVERLAY])
    overlay_python = _venv_python(OVERLAY)
    with open(os.path.join(_site_dirs(overlay_python)[0], "_apifix_deps.pth"), "w") as f:
        for d in _site_dirs(_venv_python(deps_venv)):
            f.write(f"import site; site.addsitedir({d!r})\n")

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [os.path.join(OVERLAY, "bin"), os.path.join(deps_venv, "bin"), env.get("PATH", "")]
    )
    method, log = "pythonpath", NO_PROJECT
    if any(os.path.exists(os.path.join(WORK, f)) for f in ("pyproject.toml", "setup.py")):
        pip = [overlay_python, "-m", "pip", "install", "--no-deps", "--no-build-isolation"]
        pip += ["--no-index", "-e", WORK]
        proc = subprocess.run(
            pip, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=WORK
        )
        log = proc.stdout.decode("utf-8", "replace")[-MAX_LOG:]
        if proc.returncode == 0:
            method = "editable"
    # Import paths the test plan chose (dirs whose sibling modules tests import by bare name),
    # then, without an editable install, the repo itself.
    entries = [os.path.join(WORK, p) for p in paths]
    if method == "pythonpath":
        entries += [os.path.join(WORK, "src"), WORK]
    entries = [p for p in entries if os.path.isdir(p)]
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    print("[apifix] repo install method: " + method, file=sys.stderr, flush=True)

    command = [overlay_python if part == "{python}" else part for part in command]
    coverage = coverage and command[1:2] == ["-m"]  # wrap `python -m <runner> ...` only
    if coverage:
        env["COVERAGE_FILE"] = os.path.join(RESULTS, ".coverage")
        command = [command[0], "-m", "coverage", "run", "--source", WORK, "-m"] + command[2:]
    data = {"install_method": method, "install_log": log, "command": command}
    data["import_paths"] = list(paths)
    _write_results(data)  # written before the tests run, in case they time out
    rc = subprocess.call(command, env=env, cwd=WORK)
    data["exit_code"] = rc
    if coverage:
        report = [overlay_python, "-m", "coverage", "json", "--ignore-errors"]
        report += ["-o", os.path.join(RESULTS, "coverage.json")]
        data["coverage_exit_code"] = subprocess.call(
            report, env=env, cwd=WORK, stdout=subprocess.DEVNULL
        )
    _write_results(data)
    return rc


_SMOKE_VERSIONS = """
import json, re, sys
from importlib import metadata

def version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None

dist = sys.argv[1]
out = {"distribution": dist, "version": version(dist), "requires": {}}
if out["version"] is not None:
    for req in metadata.requires(dist) or []:
        if "extra ==" in req:
            continue
        name = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", req)
        if name:
            out["requires"][name.group(0)] = version(name.group(0))
print(json.dumps(out))
"""


def smoke(distribution):
    """Run the migration's smoke check against the installed dependencies; print one marker
    line with its outcome and the installed versions of `distribution` and what it requires."""
    python = _venv_python(os.path.join(DEPS, "venv"))
    proc = subprocess.run([python, SMOKE_CHECK], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = proc.stdout.decode("utf-8", "replace")[-MAX_LOG:]
    versions = subprocess.run(
        [python, "-c", _SMOKE_VERSIONS, distribution], stdout=subprocess.PIPE
    ).stdout.decode("utf-8", "replace")
    try:
        installed = json.loads(versions.strip().splitlines()[-1])
    except (ValueError, IndexError):
        installed = {}
    result = {"ok": proc.returncode == 0, "output": output, "installed": installed}
    print(SMOKE_MARKER + json.dumps(result), flush=True)
    return 0


def main(argv):
    if argv[:1] == ["install"]:
        return install("--constraints" in argv[1:])
    if len(argv) == 2 and argv[0] == "smoke":
        return smoke(argv[1])
    if argv[:1] == ["test"] and "--" in argv:
        split = argv.index("--")
        options, command = argv[1:split], argv[split + 1 :]
        coverage, paths, ok = False, [], True
        i = 0
        while i < len(options):
            if options[i] == "--coverage":
                coverage = True
            elif options[i] == "--path" and i + 1 < len(options):
                i += 1
                paths.append(options[i])
            else:
                ok = False
            i += 1
        if ok and command:
            return test(command, coverage=coverage, paths=paths)
    print(
        "usage: driver.py install [--constraints] | smoke <distribution>"
        " | test [--coverage] [--path DIR]... -- <cmd...>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
