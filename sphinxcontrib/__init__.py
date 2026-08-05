# PEP 420 native namespace package.
# sphinxcontrib is a namespace package shared by every sphinx-contrib
# extension. Each subpackage (sphinxcontrib.adadomain, sphinxcontrib.foo,
# etc.) is shipped as its own distribution; the umbrella ``sphinxcontrib``
# directory contains no Python code so Python 3.3+ treats it as a
# namespace package.
#
# Replaces the upstream ``__import__('pkg_resources').declare_namespace``
# boilerplate, which is deprecated in setuptools 81+ and broken in
# setuptools 83+. PEP 420 native namespaces require no special runtime
# declaration.
