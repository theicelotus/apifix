"""Runs INSIDE sandbox containers, under the target repo's Python (3.8+): keep it 3.8-compatible
and stdlib-only. The host never imports this module; it is copied into the container.

    driver.py install [--constraints]   network on;  sees /manifest and /deps only
    driver.py test -- <cmd...>          network off; /deps read-only, /work = repo copy

Test phase layout: /deps is read-only, so an overlay venv in /tmp sees the dependencies via a
.pth file that calls site.addsitedir(); the repo is installed into the overlay (editable, no
network, no deps). If that fails, the repo is put on PYTHONPATH instead.
"""

import json
import os
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
    rc = subprocess.call(cmd, env=env)
    shutil.rmtree(tmp, ignore_errors=True)
    return rc


def _write_results(data):
    with open(os.path.join(RESULTS, "driver.json"), "w") as f:
        json.dump(data, f)


def test(command):
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
    method, log = "pythonpath", "no pyproject.toml or setup.py"
    if any(os.path.exists(os.path.join(WORK, f)) for f in ("pyproject.toml", "setup.py")):
        pip = [overlay_python, "-m", "pip", "install", "--no-deps", "--no-build-isolation"]
        pip += ["--no-index", "-e", WORK]
        proc = subprocess.run(
            pip, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=WORK
        )
        log = proc.stdout.decode("utf-8", "replace")[-MAX_LOG:]
        if proc.returncode == 0:
            method = "editable"
    if method == "pythonpath":
        paths = [os.path.join(WORK, "src"), WORK]
        env["PYTHONPATH"] = os.pathsep.join(p for p in paths if os.path.isdir(p))
    print("[apifix] repo install method: " + method, file=sys.stderr, flush=True)

    command = [overlay_python if part == "{python}" else part for part in command]
    data = {"install_method": method, "install_log": log, "command": command}
    _write_results(data)  # written before the tests run, in case they time out
    rc = subprocess.call(command, env=env, cwd=WORK)
    data["exit_code"] = rc
    _write_results(data)
    return rc


def main(argv):
    if argv[:1] == ["install"]:
        return install("--constraints" in argv[1:])
    if argv[:2] == ["test", "--"]:
        return test(argv[2:])
    print("usage: driver.py install [--constraints] | test -- <cmd...>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
