import importlib
import sys
import textwrap

from syncronizer.core import registry


def _make_package(tmp_path):
    pkg = tmp_path / "eptest_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "good.py").write_text(textwrap.dedent("""
        from syncronizer.core.endpoint import Endpoint
        from syncronizer.core.extract import ExtractSpec, ExtractContext

        class GoodEndpoint(Endpoint):
            name = "good"; primary_key = "id"; order = 5
            def extract_spec(self, ctx): return ExtractSpec("SELECT 1")
            def transform(self, row): return {"id": 1}
    """))
    (pkg / "later.py").write_text(textwrap.dedent("""
        from syncronizer.core.endpoint import Endpoint
        from syncronizer.core.extract import ExtractSpec, ExtractContext

        class LaterEndpoint(Endpoint):
            name = "later"; primary_key = "id"; order = 50
            def extract_spec(self, ctx): return ExtractSpec("SELECT 1")
            def transform(self, row): return {"id": 1}
    """))
    # underscore-prefixed: must be skipped WITHOUT importing (would raise if imported)
    (pkg / "_skip.py").write_text("raise RuntimeError('underscore module must not import')")
    # broken: import error must be logged + skipped, not crash discovery
    (pkg / "broken.py").write_text("import a_module_that_does_not_exist_xyz")
    sys.path.insert(0, str(tmp_path))
    return importlib.import_module("eptest_pkg")


def test_discovery_skips_underscore_and_broken_and_orders(tmp_path):
    pkg = _make_package(tmp_path)
    endpoints = registry.discover(package=pkg)
    # broken skipped, _skip skipped, ordered by (order, name)
    assert [e.name for e in endpoints] == ["good", "later"]


def test_default_package_finds_example():
    endpoints = registry.discover()
    names = [e.name for e in endpoints]
    assert "produtos" in names
    assert "TODO_name" not in names  # _template is never imported
