"""
Ada domain for sphinx.

:copyright: Copyright 2010 by Tero Koskinen.
:copyright: Copyright 2020 by AdaCore.
:license: BSD, see LICENSE for details

Some parts of the code copied from erlangdomain by SHIBUKAWA Yoshiki.
"""

from __future__ import annotations

import logging
import re
from typing import (
    Iterable, List, Protocol, Sequence, Union, cast, Any, Dict, NamedTuple,
    Iterator, Tuple
)

from docutils import nodes
from docutils.nodes import Element
from docutils.parsers.rst import directives
from docutils.parsers.rst import Directive
from docutils.parsers.rst.states import Inliner

from sphinx import addnodes
from sphinx.addnodes import desc_signature
from sphinx.application import Sphinx
from sphinx.builders import Builder
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, Index, IndexEntry, ObjType
from sphinx.environment import BuildEnvironment
from sphinx.locale import _, __
from sphinx.roles import XRefRole
from sphinx.util.docfields import Field, TypedField
from sphinx.util.nodes import make_refnode, make_id
from sphinx.util.typing import ExtensionMetadata


# ---------------------------------------------------------------------------
# Sphinx 9 / docutils 0.21+ compatibility patch
# ---------------------------------------------------------------------------
# Sphinx 9 / docutils 0.21+ requires ``desc_name(rawsource, text, *children)``;
# the older ``desc_name(text, *children)`` form puts the display text into the
# rawsource slot and leaves the visible name empty. Each ``addnodes.desc_*``
# call in this file uses the keyword ``text=`` form so the rawsource defaults
# to the empty string. See specs/03-directives-roles/01-addnodes-desc-nodes.md
# for the full addnodes hierarchy and specs/05-from-08-to-09-migration/
# 01-sphinx-9-breaking-changes.md for the migration story.
# ---------------------------------------------------------------------------
try:
    import libadalang as lal

    USE_LAL = True
    lal_context = lal.AnalysisContext(unit_provider=lal.UnitProvider.auto([]))
except ImportError:
    USE_LAL = False


# TODO: Due to the inheritance structure hierarchy of docutils nodes, and to
# limitations in mypy's protocol typing, we have no way of saying that we  want
# a node that has a constructor as below, and that is also a Node (AFAICT). For
# the moment, we'll just enforce the proper constructor
class DescNodeProtocol(Protocol):

    def __init__(self, rawsource: str = '', text: str = '',
                 *args: nodes.Element,
                 **kwargs: Any) -> None:
        pass


logger = logging.getLogger(__name__)

ada_type_sig_re = re.compile(r"^type\s+(\w+)", re.VERBOSE)
ada_object_sig_re = re.compile(r"^(\w+)\s+(:\s+.+)", re.VERBOSE)
ada_number_sig_re = re.compile(
    r"^(\w+)\s+:\s+constant\s+(\S+)", re.VERBOSE
)
ada_entry_sig_re = re.compile(
    r"^entry\s+(\w+)\s*(\(.*\))?\s*$", re.VERBOSE | re.DOTALL
)
ada_aspect_sig_re = re.compile(
    r"^(\w+)(?:\s*=>\s*(.+?))?\s+on\s+(.+)$", re.VERBOSE | re.DOTALL
)
ada_pragma_sig_re = re.compile(
    r"^(\w+)\s*(?:\((.*)\))?$", re.VERBOSE | re.DOTALL
)
ada_rep_clause_sig_re = re.compile(
    r"^for\s+(.+?)\s+use\s+(.+)$", re.VERBOSE | re.DOTALL
)
ada_with_clause_sig_re = re.compile(
    r"^with\s+(.+?)\s+on\s+(.+)$", re.VERBOSE | re.DOTALL
)
ada_package_inst_sig_re = re.compile(
    r"^package\s+(\w+)\s+is\s+new\s+([\w\.]+)\s*", re.VERBOSE
)
ada_subp_sig_re = re.compile(
    r"^(procedure|function)\s+(\w+|\".*?\")\s*(.*)", re.VERBOSE
)

ObjectEntry = NamedTuple(
    "ObjectEntry", [("docname", str), ("node_id", str), ("objtype", str)]
)


class AdaObject(ObjectDescription):
    """
    Description of an Ada language object.
    """

    doc_field_types = [
        TypedField(
            "parameter",
            label=_("Parameters"),
            names=("param", "parameter", "arg", "argument"),
            typerolename="type",
            typenames=("type",),
        ),
        TypedField(
            "component",
            label=_("Components"),
            names=("component", "comp"),
            typerolename="type",
            typenames=("type",),
        ),
        TypedField(
            "discriminant",
            label=_("Discriminants"),
            names=("discriminant", "discr"),
            typerolename="type",
            typenames=("type",),
        ),
        Field(
            "returnvalue",
            label=_("Returns"),
            has_arg=False,
            names=("returns", "return"),
        ),
        Field(
            "instpkg",
            label=_("Instantiated generic package"),
            has_arg=False,
            names=("instpkg",),
            bodyrolename="type",
        ),
        Field(
            "defval",
            label=_("Default value"),
            has_arg=False,
            names=("defval",),
            bodyrolename="type",
        ),
        Field(
            "pragmas",
            label=_("Pragmas"),
            has_arg=False,
            names=("pragmas", "pragma"),
        ),
        Field(
            "representation",
            label=_("C representation"),
            has_arg=False,
            names=("representation", "repr"),
        ),
        Field(
            "formal_kind",
            label=_("Formal kind"),
            has_arg=False,
            names=("formal_kind",),
        ),
        Field(
            "is_null",
            label=_("Null body"),
            has_arg=False,
            names=("is_null", "null_body"),
        ),
        Field(
            "renames_target",
            label=_("Renames target"),
            has_arg=False,
            names=("renames_target",),
        ),
        Field(
            "spark_mode",
            label=_("SPARK mode"),
            has_arg=False,
            names=("spark_mode",),
        ),
        Field(
            "pre",
            label=_("Precondition"),
            has_arg=False,
            names=("pre", "precondition"),
        ),
        Field(
            "post",
            label=_("Postcondition"),
            has_arg=False,
            names=("post", "postcondition"),
        ),
        Field(
            "contract_cases",
            label=_("Contract cases"),
            has_arg=False,
            names=("contract_cases",),
        ),
        Field(
            "inline",
            label=_("Inline"),
            has_arg=False,
            names=("inline",),
        ),
        Field(
            "global",
            label=_("Global"),
            has_arg=False,
            names=("global",),
        ),
        Field(
            "no_return",
            label=_("No return"),
            has_arg=False,
            names=("no_return",),
        ),
        Field(
            "convention",
            label=_("Convention"),
            has_arg=False,
            names=("convention",),
            bodyrolename="type",
        ),
        Field(
            "import_kind",
            label=_("Import"),
            has_arg=False,
            names=("import", "import_kind"),
        ),
        Field(
            "external",
            label=_("External"),
            has_arg=False,
            names=("external",),
        ),
        Field(
            "link_name",
            label=_("Link name"),
            has_arg=False,
            names=("link_name",),
        ),
        Field(
            "body",
            label=_("Body"),
            has_arg=False,
            names=("body",),
        ),
        Field(
            "proof_status",
            label=_("Proof status"),
            has_arg=False,
            names=("proof_status",),
        ),
        TypedField(
            "aspect",
            label=_("Aspect"),
            names=("aspect",),
            typerolename="type",
            typenames=("type",),
        ),
        Field(
            "objtype",
            label=_("Object type"),
            has_arg=False,
            names=("objtype",),
            bodyrolename="type",
        ),
        Field(
            "renames",
            label=_("Renames"),
            has_arg=False,
            names=("renames",),
            bodyrolename="type",
        ),
    ]

    option_spec = {
        "package": directives.unchanged,
    }

    def get_full_name(self, signode: desc_signature, name: str) -> str:
        """
        Get the full name for this Ada object.
        """
        env_modname = self.options.get(
            "package",
            self.env.temp_data.get("ada:package", "")
        )
        fullname = env_modname + "." + name if env_modname else name

        signode["package"] = env_modname
        signode["fullname"] = fullname

        return fullname

    def make_refnode(
        self,
        target: str,
        cont_node_type: type[DescNodeProtocol]
    ) -> addnodes.pending_xref:
        refnode = addnodes.pending_xref(
            "",
            refdomain="ada",
            refexplicit=False,
            reftype="type",
            reftarget=target,
        )
        env_modname = self.options.get(
            "package", self.env.temp_data.get("ada:package", "")
        )
        refnode["ada:package"] = env_modname
        refnode += cont_node_type("", target)
        return refnode

    def handle_subp_sig(self, sig: str, signode: desc_signature) -> str:

        # libadalang's subp_spec_rule requires a leading 'procedure'/'function'
        # keyword in the input buffer. The Sphinx directive name already encodes
        # the objtype, so the natural keyword-less form
        #     .. ada:procedure:: Pump_Bytes (Fd : Integer)
        # would otherwise fail with `Expected 'function', got Identifier`.
        # Prepend the keyword when it's missing, using self.objtype to know
        # which one to inject.
        m = ada_subp_sig_re.match(sig)
        if m is None:
            sig = f"{self.objtype} {sig}"

        subp_spec_unit = lal_context.get_from_buffer(
            "<input>", sig, rule=lal.GrammarRule.subp_spec_rule
        )
        subp_spec: lal.SubpSpec = subp_spec_unit.root.cast(lal.SubpSpec)

        if subp_spec is None:
            raise self.error("Couldn't parse the subp spec")

        if len(subp_spec_unit.diagnostics) > 0:
            raise self.error("Errors parsing the subp spec")

        is_func = subp_spec.f_subp_returns is not None

        subp_name, returntype = (
            subp_spec.f_subp_name.text,
            subp_spec.f_subp_returns.text if is_func else ""
        )

        kind = "function " if is_func else "procedure "
        signode += addnodes.desc_annotation(text=kind)
        signode += addnodes.desc_name(text=subp_name)

        signode += nodes.Text(" ")

        param_list = addnodes.desc_parameterlist()
        param_list.child_text_separator = "; "
        signode += param_list

        if subp_spec.f_subp_params:
            for p in subp_spec.f_subp_params.f_params:
                param = addnodes.desc_parameter()
                param_list += param
                for i, name in enumerate(p.f_ids):
                    param += addnodes.desc_sig_name("", name.text)
                    if i + 1 < len(p.f_ids):
                        param += addnodes.desc_sig_punctuation("", ", ")
                param += addnodes.desc_sig_punctuation("", " : ")

                refnode = self.make_refnode(
                    p.f_type_expr.text, addnodes.desc_sig_name
                )
                param += refnode

        if returntype:
            signode += self.make_refnode(returntype, addnodes.desc_returns)

        return subp_name

    def handle_type_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse an Ada type declaration.

        libadalang is tried first; the regex ``ada_type_sig_re`` is the
        fallback. The consumer's signature does not include the ``type``
        keyword (the directive name carries it), so we prepend it before
        asking libadalang to parse, mirroring the pattern used by
        ``handle_subp_sig``.
        """
        name: Union[str, None] = None

        # libadalang-first.
        if USE_LAL:
            try:
                # libadalang's type_decl_rule needs a complete type
                # declaration. The consumer's sig is just the bare name
                # (e.g. ``Color_T``), so we synthesise an Ada 2022
                # incomplete type declaration ``type <name>;`` and parse
                # that. Returns ``IncompleteTypeDecl`` whose ``f_name``
                # carries the identifier.
                prefixed = sig if sig.lstrip().startswith("type ") else f"type {sig};"
                # Use a unique buffer name per call to avoid state leak
                # across invocations of handle_signature in the same build.
                unit = lal_context.get_from_buffer(
                    f"<ada_type_{id(self)}>", prefixed, rule=lal.GrammarRule.type_decl_rule
                )
                root = unit.root
                if root is not None and not unit.diagnostics and root.f_name:
                    name = root.f_name.text
            except Exception:
                name = None

        # Regex fallback.
        if name is None:
            m = ada_type_sig_re.match(sig)
            if m is None:
                raise Exception(f"m did not match for sig {sig}")
            name = m.groups()[0]

        signode += addnodes.desc_annotation(text="type ")
        signode += addnodes.desc_name(text=name)
        # desc_type intentionally omitted: it produces
        # "unknown node type" warnings against Sphinx 9
        # without changing the cross-link ID or text.

        return name

    def handle_object_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse an Ada object declaration (variable or constant).

        libadalang is tried first; the regex ``ada_object_sig_re`` is the
        fallback. libadalang needs the declaration wrapped in a package
        spec to parse it standalone.
        """
        name: Union[str, None] = None
        descr: Union[str, None] = None

        # libadalang-first: wrap the bare decl in a package spec and
        # parse with package_decl_rule. We then walk into the public
        # part to find the first ObjectDecl.
        if USE_LAL:
            try:
                wrapped = f"package Wrap is {sig}; end Wrap;"
                # Use a unique buffer name per call to avoid libadalang
                # caching state across invocations.
                unit = lal_context.get_from_buffer(
                    f"<ada_obj_{id(self)}>", wrapped, rule=lal.GrammarRule.package_decl_rule
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if isinstance(decl, lal.ObjectDecl) and decl.f_ids:
                        name = decl.f_ids[0].text
                        # Rebuild the type annotation: `` : T [:= default]``.
                        type_text = (
                            decl.f_type_expr.text
                            if decl.f_type_expr is not None
                            else ""
                        )
                        default_text = (
                            decl.f_default_expr.text
                            if decl.f_default_expr is not None
                            else ""
                        )
                        if default_text:
                            descr = f": {type_text} := {default_text}"
                        else:
                            descr = f": {type_text}"
            except Exception:
                name = None
                descr = None

        # Regex fallback.
        if name is None:
            m = ada_object_sig_re.match(sig)
            if m is None:
                raise Exception(f"could not parse object sig {sig!r}")
            name, descr = m.groups()

        assert descr is not None
        descr = " " + descr

        signode += addnodes.desc_name(text=name)
        signode += addnodes.desc_annotation(text=descr)
        # desc_type intentionally omitted: it produces
        # "unknown node type" warnings against Sphinx 9
        # without changing the cross-link ID or text.

        return name

    def handle_gen_package_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a generic package declaration.

        libadalang is tried first; the raw ``sig`` is the fallback when
        libadalang cannot parse it.
        """
        name: Union[str, None] = None

        # libadalang-first: wrap in a package spec so the generic decl
        # is a recognisable inner declaration.
        if USE_LAL:
            try:
                wrapped = f"package Wrap is {sig}; end Wrap;"
                # Use a unique buffer name per call to avoid libadalang
                # caching state across invocations.
                unit = lal_context.get_from_buffer(
                    f"<ada_genpkg_{id(self)}>",
                    wrapped,
                    rule=lal.GrammarRule.package_decl_rule,
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if (
                        isinstance(decl, lal.GenericPackageDecl)
                        and decl.f_package_decl is not None
                        and decl.f_package_decl.f_package_name
                    ):
                        name = decl.f_package_decl.f_package_name.text
            except Exception:
                name = None

        if name is None:
            name = sig

        signode += addnodes.desc_annotation(text="generic package ")
        signode += addnodes.desc_name(text=name)
        return name

    def handle_package_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a package declaration.

        libadalang is tried first; the raw ``sig`` is the fallback.
        """
        name: Union[str, None] = None

        # libadalang-first.
        if USE_LAL:
            try:
                wrapped = f"package Wrap is package {sig} is end {sig}; end Wrap;"
                unit = lal_context.get_from_buffer(
                    f"<ada_pkg_{id(self)}>",
                    wrapped,
                    rule=lal.GrammarRule.package_decl_rule,
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if (
                        isinstance(decl, lal.PackageDecl)
                        and decl.f_package_name
                    ):
                        name = decl.f_package_name.text
            except Exception:
                name = None

        if name is None:
            name = sig

        signode += addnodes.desc_annotation(text="package ")
        signode += addnodes.desc_name(text=name)
        return name

    def handle_exception_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse an exception declaration.

        libadalang is tried first; the raw ``sig`` is the fallback.
        """
        name: Union[str, None] = None

        # libadalang-first: wrap in a package spec so the exception
        # decl is a recognisable inner declaration. The ``: exception``
        # marker is appended if the consumer's sig omitted it.
        if USE_LAL:
            try:
                tail = "" if sig.rstrip().endswith(": exception") else ": exception"
                wrapped = f"package Wrap is {sig}{tail}; end Wrap;"
                unit = lal_context.get_from_buffer(
                    f"<ada_exc_{id(self)}>",
                    wrapped,
                    rule=lal.GrammarRule.package_decl_rule,
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if isinstance(decl, lal.ExceptionDecl) and decl.f_ids:
                        name = decl.f_ids[0].text
            except Exception:
                name = None

        if name is None:
            name = sig

        signode += addnodes.desc_name(text=name)
        signode += addnodes.desc_annotation(text=": exception")
        return name

    def handle_number_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse an Ada number (named constant) declaration.

        Signature shape: ``Name : constant Type`` or
        ``Name : constant Type := value``. The ``: constant``
        marker is mandatory; it is what makes this directive
        different from ``ada:object``. Without ``constant`` the
        consumer is signalling a variable, and ``ada:object`` is
        the right directive.
        """
        name: Union[str, None] = None
        descr: Union[str, None] = None

        # libadalang-first: wrap in a package spec and parse
        # with package_decl_rule. Walk into the public part and
        # pick the first ObjectDecl whose ``f_is_constant`` is
        # True. ``f_default_expr`` may be present (constants
        # usually have one) but is not required.
        if USE_LAL:
            try:
                tail = " := 0" if ":=" not in sig else ""
                wrapped = f"package Wrap is {sig}{tail}; end Wrap;"
                unit = lal_context.get_from_buffer(
                    f"<ada_num_{id(self)}>",
                    wrapped,
                    rule=lal.GrammarRule.package_decl_rule,
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if isinstance(decl, lal.ObjectDecl) and decl.f_ids:
                        name = decl.f_ids[0].text
                        if decl.f_type_expr is not None:
                            type_text = decl.f_type_expr.text
                            if decl.f_default_expr is not None:
                                descr = (
                                    f" : constant {type_text} "
                                    f":= {decl.f_default_expr.text}"
                                )
                            else:
                                descr = f" : constant {type_text}"
            except Exception:
                name = None
                descr = None

        # Regex fallback.
        if name is None:
            m = ada_number_sig_re.match(sig)
            if m is None:
                raise Exception(f"could not parse number sig {sig!r}")
            name = m.group(1)
            descr = sig[len(name):]

        assert descr is not None
        if not descr.startswith(" "):
            descr = " " + descr

        signode += addnodes.desc_name(text=name)
        signode += addnodes.desc_annotation(text=descr)
        return name

    def handle_entry_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a protected entry declaration.

        Signature shape: ``entry Name`` optionally followed by
        a parenthesised parameter list. The ``entry`` keyword
        is required because the directive name is just ``entry``,
        not ``ada:entry`` (the ``ada:`` prefix is added by Sphinx).
        """
        m = ada_entry_sig_re.match(sig)
        if m is None:
            raise Exception(f"could not parse entry sig {sig!r}")
        name, params = m.groups()
        params = params or ""

        signode += addnodes.desc_annotation(text="entry ")
        signode += addnodes.desc_name(text=name)
        if params:
            signode += addnodes.desc_annotation(text=params)
        return name

    def handle_aspect_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse an aspect declaration.

        Signature shape: ``Aspect_Name [=> value] on Target_FQN``.
        The aspect name is the desc_name (so cross-references can
        link to it), the value is the desc_annotation, and the
        target is rendered as a :ada:ref: pending xref.
        """
        m = ada_aspect_sig_re.match(sig)
        if m is None:
            raise Exception(f"could not parse aspect sig {sig!r}")
        asp_name, asp_value, target = m.groups()

        signode += addnodes.desc_name(text=asp_name)
        if asp_value:
            signode += addnodes.desc_annotation(
                text=f" => {asp_value}"
            )
        signode += addnodes.desc_annotation(text=" on ")
        # Render the target as a pending cross-reference so it
        # links to the entity the aspect is attached to.
        refnode = addnodes.pending_xref(
            "",
            refdomain="ada",
            refexplicit=False,
            reftype="ref",
            reftarget=target,
        )
        refnode += addnodes.desc_name(text=target)
        signode += refnode
        return asp_name

    def handle_pragma_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a pragma declaration.

        Signature shape: ``Pragma_Name`` or
        ``Pragma_Name (arg1, arg2, ...)``. The pragma name is
        the desc_name; the args are the desc_annotation.
        """
        m = ada_pragma_sig_re.match(sig)
        if m is None:
            raise Exception(f"could not parse pragma sig {sig!r}")
        name, args = m.groups()

        signode += addnodes.desc_annotation(text="pragma ")
        signode += addnodes.desc_name(text=name)
        if args:
            signode += addnodes.desc_annotation(text=f" ({args})")
        return name

    def handle_rep_clause_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a representation clause.

        Signature shape: ``for Target use body``. The full
        clause text is rendered as a code-style annotation
        after the target name.
        """
        m = ada_rep_clause_sig_re.match(sig)
        if m is None:
            raise Exception(f"could not parse rep-clause sig {sig!r}")
        target, body = m.groups()

        signode += addnodes.desc_annotation(text="for ")
        signode += addnodes.desc_name(text=target)
        signode += addnodes.desc_annotation(text=f" use {body}")
        return target

    def handle_with_clause_sig(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a ``with`` clause cross-reference.

        Signature shape: ``with <imported_pkg_list> on
        <owning_pkg_fqn>``. The imported package names are the
        desc_name (so each becomes a cross-reference target);
        the owning package is the desc_annotation. Each
        ``with`` clause on the source file emits one such
        directive, so readers can link to specific imports.
        """
        m = ada_with_clause_sig_re.match(sig)
        if m is None:
            raise Exception(f"could not parse with-clause sig {sig!r}")
        imports, owner = m.groups()

        signode += addnodes.desc_annotation(text="with ")
        signode += addnodes.desc_name(text=imports)
        signode += addnodes.desc_annotation(text=f" on {owner}")
        return imports

    def handle_package_inst(self, sig: str, signode: desc_signature) -> str:
        """
        Parse a generic package instantiation.

        libadalang is tried first; the regex ``ada_package_inst_sig_re`` is
        the fallback.
        """
        name: Union[str, None] = None
        inst: Union[str, None] = None

        # libadalang-first.
        if USE_LAL:
            try:
                wrapped = f"package Wrap is {sig}; end Wrap;"
                # Use a unique buffer name per call to avoid libadalang
                # caching state across invocations.
                unit = lal_context.get_from_buffer(
                    f"<ada_inst_{id(self)}>",
                    wrapped,
                    rule=lal.GrammarRule.package_decl_rule,
                )
                pkg = unit.root
                if (
                    pkg is not None
                    and not unit.diagnostics
                    and pkg.f_public_part is not None
                    and pkg.f_public_part.f_decls
                ):
                    decl = pkg.f_public_part.f_decls[0]
                    if (
                        isinstance(decl, lal.GenericPackageInstantiation)
                        and decl.f_name
                        and decl.f_generic_pkg_name
                    ):
                        name = decl.f_name.text
                        inst = decl.f_generic_pkg_name.text
            except Exception:
                name = None
                inst = None

        # Regex fallback.
        if name is None:
            m = ada_package_inst_sig_re.match(sig)
            if m is None:
                raise Exception(f"m did not match for sig {sig}")
            name, inst = m.groups()

        signode += addnodes.desc_annotation(text="package ")
        signode += addnodes.desc_name(text=name)
        signode += addnodes.desc_name(text=" ")
        signode += addnodes.desc_annotation(text=" is new ")
        signode += addnodes.desc_name(text=inst)

        return name

    def handle_signature(self, sig: str, signode: desc_signature) -> str:
        if self.objtype in ["function", "procedure"]:
            ret = self.handle_subp_sig(sig, signode)
        elif self.objtype == "type":
            ret = self.handle_type_sig(sig, signode)
        elif self.objtype == "object":
            ret = self.handle_object_sig(sig, signode)
        elif self.objtype == "number":
            ret = self.handle_number_sig(sig, signode)
        elif self.objtype == "entry":
            ret = self.handle_entry_sig(sig, signode)
        elif self.objtype == "aspect":
            ret = self.handle_aspect_sig(sig, signode)
        elif self.objtype == "pragma":
            ret = self.handle_pragma_sig(sig, signode)
        elif self.objtype == "rep_clause":
            ret = self.handle_rep_clause_sig(sig, signode)
        elif self.objtype == "with_clause":
            ret = self.handle_with_clause_sig(sig, signode)
        elif self.objtype == "exception":
            ret = self.handle_exception_sig(sig, signode)
        elif self.objtype == "generic-package-instantiation":
            ret = self.handle_package_inst(sig, signode)
        elif self.objtype == "generic_package":
            ret = self.handle_gen_package_sig(sig, signode)
        elif self.objtype == "package":
            ret = self.handle_package_sig(sig, signode)
        else:
            raise Exception(f"Unhandled ada object: {self.objtype} {sig}")

        return ret

    def get_index_text(self, name: str) -> str:
        if self.objtype == "function":
            return f"{name} (Ada function)"
        elif self.objtype == "procedure":
            return f"{name} (Ada procedure)"
        elif self.objtype == "type":
            return f"{name} (Ada type)"
        elif self.objtype == "number":
            return f"{name} (Ada constant)"
        elif self.objtype == "entry":
            return f"{name} (Ada entry)"
        elif self.objtype == "aspect":
            return f"{name} (Ada aspect)"
        elif self.objtype == "pragma":
            return f"{name} (Ada pragma)"
        elif self.objtype == "rep_clause":
            return f"{name} (Ada representation clause)"
        elif self.objtype == "with_clause":
            return f"{name} (Ada with clause)"
        else:
            return ""

    def _object_hierarchy_parts(
        self, sig_node: desc_signature
    ) -> Tuple[str, ...]:
        """
        Return the breadcrumb path for cross-reference rendering.

        Sphinx 9 introduced ``_object_hierarchy_parts`` so that a directive
        can tell Sphinx how its display name should be broken up into a
        hierarchy (e.g. ``Ada.Foo.Bar`` instead of just ``Bar``). The
        returned tuple is the breadcrumb's prefix segments; the leaf name
        is appended by Sphinx from the directive's resolved name.

        For the Ada domain we split the resolved name on ``.`` and return
        everything except the leaf. Nested generics are not handled.
        """
        # The signature line carries the full Ada dotted name
        # (e.g. "My_Package.My_Procedure"). Split on the last '.' to
        # produce the hierarchy prefix. If there is no dot the object is
        # top-level and there is no breadcrumb.
        full = sig_node.get("_toc_parts_name") or ""
        if not full:
            # Fall back to the textual content of the first desc_name child.
            for child in sig_node.children:
                if isinstance(child, addnodes.desc_name):
                    full = child.astext()
                    break
        parts = full.split(".")
        if len(parts) <= 1:
            return ()
        return tuple(parts[:-1])

    def add_target_and_index(
        self, name: str, sig: str, signode: desc_signature
    ) -> None:

        full_name = self.get_full_name(signode, name)

        node_id = make_id(self.env, self.state.document, "", full_name)
        signode["ids"].append(node_id)

        # Assign old styled node_id(full_name) not to break old hyperlinks (if
        # possible) Note: Will removed in Sphinx-5.0 (RemovedInSphinx50Warning)
        if node_id != full_name and full_name not in self.state.document.ids:
            signode["ids"].append(full_name)

        self.state.document.note_explicit_target(signode)

        domain = cast(AdaDomain, self.env.get_domain("ada"))

        domain.note_object(full_name, self.objtype, node_id, location=signode)

        indextext = self.get_index_text(full_name)
        if indextext:
            self.indexnode["entries"].append(
                ("single", indextext, node_id, "", None)
            )


class AdaSetPackage(Directive):
    """
    Directive to mark description of a new package.
    """

    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = False
    option_spec = {
        "platform": lambda x: x,
        "synopsis": lambda x: x,
        "noindex": directives.flag,
        "deprecated": directives.flag,
    }

    def run(self) -> Sequence[nodes.Node]:
        env = self.state.document.settings.env
        modname = self.arguments[0].strip()
        noindex = "noindex" in self.options
        env.temp_data["ada:package"] = modname
        env.domaindata["ada"]["packages"][modname] = (
            env.docname,
            self.options.get("synopsis", ""),
            self.options.get("platform", ""),
            "deprecated" in self.options,
        )
        targetnode = nodes.target(
            "", "", ids=["package-" + modname], ismod=True
        )
        node_id = make_id(env, self.state.document, "", modname)
        self.state.document.note_explicit_target(targetnode)
        targetnode["ids"].append(node_id)
        ret: List[nodes.Node] = [targetnode]
        # XXX this behavior of the module directive is a mess...
        if "platform" in self.options:
            platform = self.options["platform"]
            node = nodes.paragraph()
            node += nodes.emphasis("", _("Platforms: "))
            node += nodes.Text(platform, platform)
            ret.append(node)
        # the synopsis isn't printed; in fact, it is only used in the
        # modindex currently.
        if not noindex:
            indextext = _("%s (package)") % modname
            inode = addnodes.index(
                entries=[
                    ("single", indextext, "package-" + modname, modname, None)
                ]
            )
            ret.append(inode)

        # Register the module in the Ada domain index, so that we can reference
        # it.
        domain = cast(AdaDomain, env.get_domain("ada"))
        domain.note_object(modname, "module", node_id, location=targetnode)

        return ret


def rmlink(name: str, rawtext: str, text: str,
           lineno: int, inliner: Inliner, options: Dict[str, Any] = {},
           content: List[str] = []) -> Tuple[List[nodes.Node],
                                             List[nodes.system_message]]:
    """
    Role to reference an Ada Reference Manual entry, such as
    ``:ada:rmlink:`3.4.2` ``
    """
    rm_page = text.replace(".", "-")
    url = f"http://www.ada-auth.org/standards/2xrm/html/RM-{rm_page}.html"
    node = nodes.reference(rawtext, f"RM {text}", refuri=url, **options)
    return [node], []


class AdaXRefRole(XRefRole):

    def process_link(
        self,
        env: BuildEnvironment,
        refnode: Element,
        has_explicit_title: bool,
        title: str, target: str
    ) -> tuple[str, str]:

        refnode["reftype"] = "type"
        refnode["refexplicit"] = False
        refnode["refdomain"] = "ada"
        refnode["reftarget"] = target

        env_modname = self.env.temp_data.get("ada:package", "")
        refnode["ada:package"] = env_modname
        return title, target


class AdaPackageIndex(Index):
    """
    Index subclass to provide the Ada package index.
    """

    name = "modindex"
    localname = _("Ada Package Index")
    shortname = _("Ada packages")

    def generate(
        self, docnames: Union[Iterable[str], None] = None
    ) -> Tuple[List[Tuple[str, List[IndexEntry]]], bool]:

        content: Dict[str, List[IndexEntry]] = {}
        # list of prefixes to ignore
        ignores = self.domain.env.config["modindex_common_prefix"]
        ignores = sorted(ignores, key=len, reverse=True)
        # list of all modules, sorted by module name
        # (Python 3 has no iteritems, so use items).
        modules = sorted(
            self.domain.data["packages"].items(), key=lambda x: x[0].lower()
        )
        # sort out collapsable modules
        prev_modname = ""
        num_toplevels = 0
        for modname, (docname, synopsis, platforms, deprecated) in modules:
            if docnames and docname not in docnames:
                continue

            for ignore in ignores:
                if modname.startswith(ignore):
                    modname = modname[len(ignore):]
                    stripped = ignore
                    break
            else:
                stripped = ""

            # we stripped the whole module name?
            if not modname:
                modname, stripped = stripped, ""

            entries = content.setdefault(modname[0].lower(), [])

            package = modname.split(":")[0]
            if package != modname:
                # it's a submodule
                if not prev_modname.startswith(package):
                    # submodule without parent in list, add dummy entry
                    entries.append(
                        IndexEntry(stripped + package, 1, "", "", "", "", "")
                    )
                subtype = 2
            else:
                num_toplevels += 1
                subtype = 0

            qualifier = deprecated and _("Deprecated") or ""
            entries.append(
                IndexEntry(
                    stripped + modname,
                    subtype,
                    docname,
                    "package-" + stripped + modname,
                    platforms,
                    qualifier,
                    synopsis,
                )
            )
            prev_modname = modname

        # apply heuristics when to collapse modindex at page load:
        # only collapse if number of toplevel modules is larger than
        # number of submodules.
        collapse = len(modules) - num_toplevels < num_toplevels

        # sort by first letter
        # (Python 3 has no iteritems, so use items).
        list_content = sorted(content.items())

        return list_content, collapse


class AdaDomain(Domain):
    """Ada language domain."""

    name = "ada"
    label = "Ada"

    # bumped to 2 in 1.0.fork1 because ``AdaPackageIndex`` is
    # now actively used (it was defined before but never
    # populated by any consumer on the upstream master branch).
    # Bumped to 3 in 0.7 because ``self.objects`` changed
    # shape: fullname -> list[ObjectEntry] (one per overload)
    # instead of fullname -> ObjectEntry. Pre-0.7 pickled envs
    # are discarded on first build with this fork; the new
    # env is rebuilt from source.
    data_version = 3

    object_types = {
        "function": ObjType(_("function"), "func"),
        "procedure": ObjType(_("procedure"), "proc"),
        "type": ObjType(_("type"), "type"),
        "package": ObjType(_("package"), "pkg"),
        "object": ObjType(_("object"), "obj"),
        "number": ObjType(_("number"), "num"),
        "entry": ObjType(_("entry"), "entry"),
        "exception": ObjType(_("exception"), "exc"),
        "generic_package": ObjType(_("generic package"), "genpkg"),
        "generic-package-instantiation": ObjType(
            _("generic package instantiation"), "geninst"
        ),
        "aspect": ObjType(_("aspect"), "aspect"),
        "pragma": ObjType(_("pragma"), "pragma"),
        "rep_clause": ObjType(_("representation clause"), "repclause"),
        "with_clause": ObjType(_("with clause"), "withclause"),
    }

    directives = {
        "function": AdaObject,
        "procedure": AdaObject,
        "type": AdaObject,
        "set_package": AdaSetPackage,
        "package": AdaObject,
        "generic_package": AdaObject,
        "object": AdaObject,
        "number": AdaObject,
        "entry": AdaObject,
        "exception": AdaObject,
        "generic-package-instantiation": AdaObject,
        "aspect": AdaObject,
        "pragma": AdaObject,
        "rep_clause": AdaObject,
        "with_clause": AdaObject,
    }
    roles = {
        "func": AdaXRefRole(),
        "proc": AdaXRefRole(),
        "type": AdaXRefRole(),
        "ref": AdaXRefRole(),
        "mod": AdaXRefRole(),
        "rmlink": rmlink,
        # ``any`` is the catch-all cross-reference role. Sphinx
        # dispatches ``:ada:any:`Foo`` and any untyped markdown
        # link to ``Domain.resolve_any_xref`` (which iterates over
        # the domain's role namespace). Without this entry,
        # docutils rejects the role before Sphinx even gets to
        # dispatch.
        "any": AdaXRefRole(),
        # Tier-1 objtypes get their own role names so authors can
        # write ``:ada:aspect:`Inline`` etc. and have Sphinx route
        # to the right lookup.
        "num": AdaXRefRole(),
        "entry": AdaXRefRole(),
        "aspect": AdaXRefRole(),
        "pragma": AdaXRefRole(),
        "repclause": AdaXRefRole(),
        "withclause": AdaXRefRole(),
    }

    # TODO: Is this useful?
    # Type annotation on ``objects`` widens to ``Union[list,
    # ObjectEntry]`` so the defensive ``isinstance(entries,
    # list)`` checks in ``note_object`` / ``clear_doc`` type-
    # narrow correctly under Pyright. Sphinx's pickle machinery
    # only round-trips the runtime value, not the type
    # annotation, so the runtime type is always ``list`` for
    # envs built with this fork.
    initial_data: dict = {
        # Tier 3a: ``objects`` is now fullname -> list of
        # ObjectEntry (one per overload) instead of fullname ->
        # ObjectEntry. Old pickled envs from sphinxcontrib-
        # adadomain 0.5 with the singleton shape are picked up
        # by ``note_object`` defensively and migrated in place.
        # The pickled-env data_version bump below is the
        # authoritative signal to discard pre-Tier-3a envs.
        "objects": {},  # fullname -> list[ObjectEntry]
        "functions": {},  # fullname -> arity -> (targetname, docname)
        "procedures": {},  # fullname -> arity -> (targetname, docname)
        "packages": {},
        # packagename -> docname, synopsis, platform, deprecated
    }

    indices = [
        AdaPackageIndex,
    ]

    def clear_doc(self, docname: str) -> None:
        # Tier 3a: ``self.objects[name]`` is now a list of
        # ObjectEntry. We filter out entries belonging to
        # ``docname`` and rebuild the (possibly shorter) list.
        for fullname, entries in list(self.objects.items()):
            if not isinstance(entries, list):
                entries = [entries]
            keep = [e for e in entries if e.docname != docname]
            if not keep:
                del self.objects[fullname]
            elif len(keep) < len(entries):
                self.objects[fullname] = keep

    def _find_obj(
        self, env: BuildEnvironment, modname: str, name: str, objtype: str
    ) -> Tuple[str, str]:
        """
        Find a Ada object for ``name``, perhaps using the given
        module and/or classname.

        Tier 3a: a single name may now resolve to multiple
        ObjectEntry records (overloaded functions). This method
        returns the first overload it finds and its docname.
        Use ``_find_overloads`` to get the full list when
        disambiguation matters (e.g. an overload summary
        directive).
        """
        # First try: try to find an object by that name (this is
        # assuming that the user used a fully qualified name).
        # ``self.objects[name]`` is now a list of ObjectEntry.
        entries = self.objects.get(name)

        # Second try: try prefixing the object with the module
        # name.
        if entries is None:
            fqn = f"{modname}.{name}"
            entries = self.objects.get(fqn)
            if entries is not None:
                name = fqn

        if entries:
            # Take the first overload. ``resolve_xref`` does
            # not know which overload the caller wants without
            # additional context (arity, types); picking the
            # first preserves the existing behaviour for the
            # common single-overload case.
            entry = entries[0] if isinstance(entries, list) else entries
            return name, entry.docname

        return ("", "")

    def _find_overloads(
        self, env: BuildEnvironment, modname: str, name: str
    ) -> List[Tuple[str, str, str]]:
        """
        Return all overloads matching ``name`` (or
        ``modname.name`` as fallback).

        Each result is ``(fullname, docname, objtype)``.
        Returns an empty list when nothing matches.

        Tier 3a: this is the disambiguation entry point for
        the overload-aware cross-reference resolver. The
        overload summary directive uses it to render a
        ``:ada:func:`Foo`` page that lists every overload and
        links to it.
        """
        results: List[Tuple[str, str, str]] = []
        for fqn in (name, f"{modname}.{name}"):
            entries = self.objects.get(fqn)
            if entries is None:
                continue
            # Accept both list (current) and single ObjectEntry
            # (legacy pickled env).
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                results.append((fqn, entry.docname, entry.objtype))
        return results

    def resolve_xref(
        self, env: BuildEnvironment, fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: addnodes.pending_xref,
        contnode: Element
    ) -> Union[Element, None]:

        # Resolve classwide type references to their base type
        real_target = target

        # We only handle the capitalized 'Class, because it is necessarily
        # formatted with a capital when the result is generated by
        # libadalang's doc generator.
        if target.endswith("'Class"):
            real_target = target[:-6]

        modname = node.get("ada:package")
        name, obj = self._find_obj(env, modname, real_target, typ)
        if not obj:
            return None
        else:
            # If we correctly resolved the object and are able to make an
            # hyperlink, then use its relative name as a display name.

            # TODO: For some reason in old versions of Sphinx the contnode is
            # sometimes a `Text` node, which doesn't make any sense as far as I
            # understand. Ignore those cases:
            if not isinstance(contnode, nodes.Text):
                contnode[0] = nodes.Text(target.split(".")[-1])
            return make_refnode(
                builder, fromdocname, obj, name, contnode, name
            )

    def resolve_any_xref(
        self, env: BuildEnvironment, fromdocname: str,
        builder: Builder,
        target: str,
        node: addnodes.pending_xref,
        contnode: Element,
    ) -> List[Tuple[str, nodes.reference]]:
        """
        Resolve a cross-reference whose role is ``:any:`` (or otherwise
        untyped) by trying every role this domain advertises.

        Sphinx calls this for ``:any:`` cross-references when the
        consumer writes ``:ada:any:`Foo`` or when a downstream
        processor (e.g. myst-parser) lowers a markdown link to
        ``pending_xref`` without a specific role. Returning an empty
        list tells Sphinx to fall through to ``resolve_xref`` and
        finally emit its standard "undefined label" warning.

        We try every role this domain knows about, in declaration
        order, and return one ``(role_name, refnode)`` tuple per
        successful match. A single object can match at most one
        role because each Ada symbol is registered under a single
        objtype.
        """
        results: List[Tuple[str, nodes.reference]] = []

        # Strip the ``'Class`` suffix that laldoc emits for classwide
        # types; the registered name doesn't carry it.
        real_target = target[:-6] if target.endswith("'Class") else target

        # Build the search prefix the same way resolve_xref does, so
        # the cross-reference still picks up the document's current
        # package when the writer used a bare name.
        modname = node.get("ada:package")

        for objtype, objtype_def in self.object_types.items():
            # objtype_def.roles is a tuple of role names that target
            # this objtype. For the Ada domain each objtype has
            # exactly one role. We iterate so a future change that
            # adds more roles per objtype keeps working.
            for role in objtype_def.roles:
                name, docname = self._find_obj(env, modname, real_target, objtype)
                if not docname:
                    continue
                refnode = make_refnode(
                    builder, fromdocname, docname, name, contnode, name
                )
                if refnode is None:
                    continue
                results.append((f"ada:{role}", refnode))
                # One match per object is enough.
                break

        return results

    def get_objects(self) -> Iterator[Tuple[str, str, str, str, str, int]]:
        # Tier 3a: ``self.objects[name]`` is now a list of
        # ObjectEntry. We yield one inventory entry per
        # overload so all overloads are reachable from
        # intersphinx consumers. The ``refname`` is qualified
        # with the node_id when there are multiple overloads
        # so inventory consumers can disambiguate. Without the
        # suffix, two overloads of ``Foo`` would collide in
        # the inventory.
        for refname, entries in self.objects.items():
            if not isinstance(entries, list):
                entries = [entries]
            for idx, obj in enumerate(entries):
                if len(entries) > 1:
                    qualified = f"{refname}#{obj.node_id}"
                else:
                    qualified = refname
                yield (
                    qualified,
                    refname,
                    obj.objtype,
                    obj.docname,
                    obj.node_id,
                    1,
                )

    @property
    def objects(self) -> Dict[str, Union[List[ObjectEntry], ObjectEntry]]:
        # The dict maps ``fullname`` to either a list of
        # ObjectEntry (one per overload, current shape) or a
        # single ObjectEntry (legacy pickled env from
        # sphinxcontrib-adadomain < 0.7). ``note_object`` /
        # ``clear_doc`` migrate single-ObjectEntry values to
        # lists on first access. ``get_objects`` accepts both
        # shapes.
        return self.data.setdefault(
            "objects", {}
        )  # fullname -> list[ObjectEntry] | ObjectEntry

    def note_object(
        self, name: str, objtype: str, node_id: str, location: Any = None
    ) -> None:
        """
        Note an ada object for cross references.

        Tier 3a: when a name is overloaded (e.g. two
        ``Foo`` functions with different parameter lists), the
        upstream implementation warns and overwrites; the
        second overload becomes unreachable via
        ``:ada:func:`Foo```. We instead keep a list of all
        overloads under the same name. ``_find_obj`` returns
        the first overload; ``_find_overloads`` returns the
        full list for callers that need to disambiguate.

        ``self.objects`` now maps fullname -> list of
        ObjectEntry. Older callers that indexed
        ``self.objects[name]`` expecting an ObjectEntry
        directly have been updated to take ``[0]`` of the
        list.
        """
        new_entry = ObjectEntry(self.env.docname, node_id, objtype)
        existing = self.objects.get(name)
        if existing is None:
            self.objects[name] = [new_entry]
            return
        # Defensive: accept either a list (current) or a
        # single ObjectEntry (legacy pickled env from before
        # Tier 3a).
        if isinstance(existing, list):
            existing.append(new_entry)
        else:
            logger.warning(
                __(
                    "duplicate object description of %s, "
                    "other instance in %s, use :noindex: for one of them"
                ),
                name,
                existing.docname,
            )
            self.objects[name] = [existing, new_entry]

    @staticmethod
    def add_missing_reference(
        app: Sphinx,
        env: BuildEnvironment,
        node: addnodes.pending_xref,
        contnode: Element,
    ) -> Union[Element, None]:
        """
        Suggest a close match when an :ada: cross-reference cannot be
        resolved.

        Sphinx calls registered ``missing-reference`` handlers with the
        signature ``(app, env, node, contnode)`` (the ``app`` argument is
        passed implicitly when handlers are connected as bound methods).
        Returning a non-``None`` node replaces the unresolved-ref rendering
        in the output; returning ``None`` falls through to Sphinx's
        default "undefined label" warning.

        For the Ada domain we look up the requested name in the domain's
        ``objects`` table and, if found, render the resolved
        cross-reference. This catches the common case where the user
        writes ``:ada:func:`Foo`` in a document whose current package is
        ``Ada.Pkg`` and the actual target is ``Ada.Pkg.Foo`` -- Sphinx's
        stock resolver will already have tried the current-package prefix
        via ``resolve_xref`` and failed; we re-attempt here with a wider
        search.
        """
        domain = env.get_domain("ada")
        target = node.get("reftarget", "")
        objects = domain.objects

        # First try: exact match.
        if target in objects:
            return None  # Already resolvable; let Sphinx handle it.

        # Second try: same name with the document's current package prefix.
        modname = node.get("ada:package", "")
        if modname:
            candidate = f"{modname}.{target}"
            if candidate in objects:
                return make_refnode(
                    app.builder,
                    node.get("refdoc", ""),
                    objects[candidate].docname,
                    candidate,
                    contnode,
                    candidate,
                )

        # Third try: any object whose name ends with the unresolved target.
        # Returns the first match (alphabetical) as a suggestion. This is
        # the "did you mean ...?" behaviour.
        for fullname in sorted(objects):
            if (
                fullname.endswith(f".{target}")
                or fullname == target
                or fullname.endswith(target)
            ):
                if not isinstance(contnode, nodes.Text):
                    contnode[0] = nodes.Text(target.split(".")[-1])
                return make_refnode(
                    app.builder,
                    node.get("refdoc", ""),
                    objects[fullname].docname,
                    fullname,
                    contnode,
                    fullname,
                )

        # No suggestion; let Sphinx emit its default warning.
        return None


def setup(app: Sphinx) -> ExtensionMetadata:
    app.require_sphinx("9.0")
    app.add_domain(AdaDomain)
    app.connect("missing-reference", AdaDomain.add_missing_reference)
    return {
        "version": "1.0.fork1",
        "parallel_read_safe": True,
        "parallel_write_safe": True,
        "env_version": 2,
    }
