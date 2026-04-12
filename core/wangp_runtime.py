"""WanGP module loader — dynamic import of wgp.py with environment setup.

Direct copy from Github Video Editor/wangp_runtime.py (identical across
Fun-Videos, BRIDGES, and Github Video Editor).
"""
from contextlib import contextmanager
import importlib
import os
import sys
import types


def _prepend_sys_path(path):
    if path and path not in sys.path:
        sys.path.insert(0, path)


def prepare_wangp_runtime(app_path):
    """Set up sys.path and PATH for WanGP imports.

    Returns (app_path, wgp_path) as absolute strings.
    """
    app_path = os.path.abspath(app_path)
    wgp_path = os.path.join(app_path, "wgp.py")
    if not os.path.isfile(wgp_path):
        raise RuntimeError(f"Cannot find wgp.py in {app_path}")

    site_packages = os.path.join(app_path, "env", "Lib", "site-packages")
    scripts_dir = os.path.join(app_path, "env", "Scripts")

    if os.path.isdir(scripts_dir):
        os.environ["PATH"] = scripts_dir + os.pathsep + os.environ.get("PATH", "")

    _prepend_sys_path(app_path)
    if os.path.isdir(site_packages):
        _prepend_sys_path(site_packages)

    return app_path, wgp_path


def load_wangp_module(app_path, ensure_app_placeholder=False):
    """Dynamically import wgp.py and return the module object."""
    app_path, wgp_path = prepare_wangp_runtime(app_path)
    original_argv = sys.argv[:]
    original_cwd = os.getcwd()
    try:
        os.chdir(app_path)
        sys.argv = [wgp_path]
        wgp = importlib.import_module("wgp")
    finally:
        sys.argv = original_argv
        os.chdir(original_cwd)

    if ensure_app_placeholder and not hasattr(wgp, "app"):
        wgp.app = types.SimpleNamespace()
    return wgp


@contextmanager
def wangp_app_context(app_path):
    """Context manager that chdirs into the WanGP app directory."""
    original_cwd = os.getcwd()
    os.chdir(os.path.abspath(app_path))
    try:
        yield
    finally:
        os.chdir(original_cwd)
