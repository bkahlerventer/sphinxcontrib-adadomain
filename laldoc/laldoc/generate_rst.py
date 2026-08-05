#! /usr/bin/env python
"""
This module forms the basis of the "new" laldoc. It is completely
distinct from the ``AutoPackage`` directive found in __init__.py.

It depends on the reworked Ada domain that can be found at
<https://github.com/AdaCore/sphinxcontrib-adadomain>.

This module is a command line program that will generate RST files from an Ada
project, given a list of API files to take into account.

We might want to add a sphinx directive like in the old laldoc at some stage,
but having it separate demonstrates how we could integrate such a mechanism
into GNATDoc for example, in pure Ada.

The workflow for the moment is hence to:

1. Generate the RST files as part of the doc build
2. Run sphinx make on the resulting doc, making sure to install
``sphinxcontrib.adadomain`` and add it to the list of extensions.
"""


from collections import defaultdict
from contextlib import contextmanager
import os
from os import path as P
import re
import sys
from typing import Dict, List, Optional as Opt, Set, Tuple, Union

import libadalang as lal


PY3 = sys.version_info[0] == 3
if PY3:
    from functools import lru_cache as memoize
else:
    from funcy import memoize


UNDERLINES = ["-", "^", "\""]


def is_documentable_subp(node: lal.BasicDecl):
    """
    Return whether ``node`` is part of the class of subprogram declarations we
    want to document.
    """
    return node.is_a(
        lal.BasicSubpDecl, lal.ExprFunction,
        lal.SubpRenamingDecl, lal.NullSubpDecl
    )


def strip_ws(strn: str) -> str:
    """
    Strip whitespace from ``strn``.
    """
    return re.sub(r"\s+", " ", strn)


class GenerateDoc(lal.App):
    """
    Main class for the documentation generator app, using the lal.App
    base class.
    """

    annotations = {
        'no-document': bool,  # Don't document associated entity

        'belongs-to': str,  # Attach associated entity to entity given in
                            # parameter

        'document-value': bool,  # Whether to document the value of an object
                                 # decl or not. Default is True
    }

    lines: List[str]
    _indent: int
    _package_nesting_level: int

    def add_string(self, strn: str):
        """
        Add ``strn`` to the rst output.
        """
        self.add_lines(strn.splitlines())

    def add_lines(self, lines: List[str]):
        """
        Add given lines to the rst output.
        """
        for line in lines:
            self.lines.append(f"{' ' * self._indent}{line}")

    def add_arguments(self):
        self.parser.add_argument(
            '-O', '--output-dir', type=str,
            default=".",
            help='Output directory for the generated rst files'
        )
        # Add additional source directories to the GPR project at
        # command-line time. The default laldoc workflow requires
        # every subdirectory holding ``.ads`` files to be listed in
        # the GPR's ``Source_Dirs``; this argument lets callers add
        # subdirs without rewriting the GPR. Useful for projects
        # whose source tree is reorganised frequently.
        self.parser.add_argument(
            '--source-dir', action='append', default=[],
            metavar='DIR',
            help='Add DIR to the GPR project Source_Dirs at run '
                 'time. May be passed multiple times.'
        )
        super(GenerateDoc, self).add_arguments()

    @contextmanager
    def indent(self):
        """
        Context manager to indent sphinx code emitted inside the with block.
        """
        try:
            self._indent += 4
            yield
        finally:
            self._indent -= 4

    @staticmethod
    def error(error_message: str):
        """
        Print an error message and exit.
        """
        print(error_message)
        exit(1)

    @staticmethod
    def warn(error_message: str):
        """
        Print a warning message.
        """
        print(error_message)

    def process_annotation(
        self, key: str, value: str
    ) -> Union[bool, str, None]:
        """
        Process an annotation for an entity, return its structured value.
        """
        try:
            atype = self.annotations[key]
        except KeyError:
            self.warn(f'Unknown annotation: {key}')
            return

        if atype is bool:
            return {'True': True, 'False': False}[value]
        elif atype is str:
            return value
        else:
            assert False

    @staticmethod
    def process_docstring(strn: str) -> str:
        """
        Docstring preprocessing, handling laldoc specific syntax:

        * Transform ``@var`` annotations into  ``:ada:ref:var`` annotations.
        """
        # Split the doc on `` literals to avoid doing substitutions of @ syntax
        # inside inline literal blocks.
        split_doc = re.split(r"(``.+?``)", strn)
        for i, chunk in enumerate(split_doc):
            if chunk.startswith('`'):
                continue

            split_doc[i] = re.sub(
                r"@([\w.]+)",
                lambda m: f":ada:ref:`{m.groups()[0]}`",
                chunk
            )
        return "".join(split_doc)

    @memoize
    def get_documentation(
        self, decl: lal.BasicDecl
    ) -> Tuple[List[str], Dict[str, str]]:
        """
        Return the documentation for given basic declaration.

        The returned tuple contains:

        1. the list of lines that constitutes the documentation for ``decl``;
        2. a mapping (key: string, value: string) for the parsed annotations.
        """
        try:
            doc = self.process_docstring(decl.p_doc).splitlines()
            annots = {a.key: self.process_annotation(a.key, a.value)
                      for a in decl.p_doc_annotations}
        except lal.PropertyError:
            self.warn('Badly formatted doc for {}'.format(decl.entity_repr))
            return [], {}

        return doc, annots

    def main(self) -> None:
        self.lines = []
        self._indent = 0
        self._package_nesting_level = 0

        os.makedirs(self.args.output_dir, exist_ok=True)

        # Sort unit by filename to have a deterministic processing order
        for _, unit in sorted(self.units.items()):
            self.process_unit(unit)

    def default_get_files(self):
        # Override libadalang's App.default_get_files to honour our
        # ``--source-dir`` flags. The base implementation returns the
        # GPR project's source files; the override instead walks every
        # ``--source-dir`` and yields every ``.ads``/``.adb`` under
        # them, regardless of depth. This avoids the "GPR doesn't
        # recurse into subdirs" pitfall and lets callers point laldoc
        # at an existing source tree without rewriting the GPR.
        if self.args.source_dir:
            extras: List[str] = []
            for d in self.args.source_dir:
                for root, _, files in os.walk(d):
                    for fn in sorted(files):
                        if fn.endswith('.ads') or fn.endswith('.adb'):
                            extras.append(os.path.join(root, fn))
            return extras
        return super().default_get_files()

    @property
    def description(self) -> str:
        return """
        Generate ReST output from Ada files. Only files containing
        library level packages are handled
        """

    def process_unit(self, unit: lal.AnalysisUnit) -> None:
        """
        Process one LAL analysis unit.
        """
        if unit.diagnostics:
            self.error('Parsing error in {}'.format(unit.filename))
            for diag in unit.diagnostics:
                self.error(
                    "{}:{}".format(str(diag.sloc_range.start), diag.message)
                )

        if not unit.root:
            self.error('{} is empty'.format(unit.filename))

        try:
            # The libadalang root is either a ``CompilationUnit``
            # (single library unit per file) or a
            # ``CompilationUnitList`` (multiple library units per
            # file, e.g. a thick instantiation followed by
            # additional declarations). We only process the FIRST
            # ``CompilationUnit``'s decl because laldoc emits one
            # RST page per source file.
            root = unit.root
            if root.is_a(lal.CompilationUnitList):
                first_cu = None
                for cu in root:
                    first_cu = cu
                    break
                if first_cu is None:
                    self.error('{} has no compilation units'.format(
                        unit.filename
                    ))
                    return
                f_body = first_cu.f_body
            else:
                f_body = root.cast(lal.CompilationUnit).f_body
            decl = f_body.cast(lal.LibraryItem).f_item

            if decl.is_a(lal.GenericPackageDecl):
                # Thin generic template at the top of a library
                # unit. ``handle_package`` emits the title and the
                # ``.. ada:set_package::`` directive; the
                # ``.. ada:generic_package::`` directive for the
                # template itself is emitted by handle_package
                # when it sees the ``gen_package`` argument is
                # non-None (see below).
                gen_package = decl.cast(lal.GenericPackageDecl)
                package_decl = gen_package.f_package_decl
                self.handle_package(package_decl, gen_package)
            elif decl.is_a(lal.GenericPackageInstantiation):
                # Top-level library unit whose package declaration is
                # itself a generic package instantiation. The thin
                # template's name is exposed via
                # ``decl.f_generic_pkg_name`` and the actuals via
                # ``decl.f_params``. We emit the template's body via
                # ``handle_package`` so any nested decls in the
                # instantiation's source file are documented too.
                inst = decl.cast(lal.GenericPackageInstantiation)
                self.handle_instantiation(inst)
            else:
                package_decl = decl.cast(lal.BasePackageDecl)
                self.handle_package(package_decl)

            out_file = P.join(self.args.output_dir,
                              P.basename(P.splitext(unit.filename)[0]))
            with open(f"{out_file}.rst", "w") as f:
                f.write("\n".join(line.rstrip() for line in self.lines))

            self.lines = []
        except AssertionError:
            print(f"WARNING: Non handled top level decl: {decl}")
            return

    def handle_instantiation(
        self,
        inst: lal.GenericPackageInstantiation
    ) -> None:
        """
        Handle a top-level library unit that is itself a generic
        package instantiation. Emit the same
        ``ada:generic-package-instantiation`` directive as the
        nested case, plus the package-level doc.

        Each RST page Sphinx consumes must have a title set off
        by ``===`` underline characters; laldoc emits these titles
        from ``p_fully_qualified_name`` for ordinary packages and
        from the unit's ``f_item`` decl for instantiations.
        """
        title = inst.p_fully_qualified_name
        self.add_lines([title, '=' * len(title), ''])

        pkg_doc, _ = self.get_documentation(inst)
        if pkg_doc:
            self.add_lines(pkg_doc)
            self.add_lines([''])

        sig = strip_ws(lal.Token.text_range(
            inst.token_start,
            (inst.f_generic_pkg_name.token_end
             if inst.f_generic_pkg_name else inst.token_end)
        ))
        self.add_lines([f".. ada:generic-package-instantiation:: {sig}"])
        with self.indent():
            self.add_lines([''])
            self.add_string(".. code-block:: ada")
            with self.indent():
                self.add_lines([''] + inst.text.splitlines())
            self.add_lines([''])
            # Resolve the instantiated generic package back to
            # its template. ``p_designated_generic_decl`` is the
            # semantic path but it has two failure modes we work
            # around here:
            #   1. cross-file resolution raises ``PropertyError``
            #      "dereferencing a null access" in libadalang 26.0.0
            #      when the generic lives in a different file with no
            #      body stub on disk.
            #   2. with a GPR-aware unit provider the property
            #      sometimes returns the right node but
            #      ``p_fully_qualified_name`` returns the
            #      instantiation's own name instead of the generic's
            #      name (observed on the smoke project;
            #      ``f_package_decl.f_package_name`` returns the
            #      correct generic-template name in that case).
            # In both cases ``f_generic_pkg_name.text`` is syntactic
            # and always correct, so we use it as the authoritative
            # source and only fall back to semantic resolution when
            # the syntactic field is null.
            if inst.f_generic_pkg_name:
                generic_fqn = inst.f_generic_pkg_name.text
            else:
                try:
                    generic_fqn = (
                        inst.p_designated_generic_decl
                        .f_package_decl.f_package_name.text
                    )
                except (lal.PropertyError, AttributeError):
                    generic_fqn = "?"
            self.add_string(f":instpkg: {generic_fqn}")

    def handle_package(
        self,
        package_decl: lal.BasePackageDecl,
        gen_package: Opt[lal.GenericPackageDecl] = None
    ) -> None:
        """
        Handle a package declaration. This method is called recursively, to
        share code, and will have a different behavior when the package is a
        toplevel one or a nested one.
        """
        # Each declaration can group the documentation of several other
        # declarations. This mapping (decl -> list[decl]) describes this
        # grouping.
        associated_decls = defaultdict(list)

        # List of top-level declarations to document
        toplevel_decls = []

        # Set mirroring toplevel_decls, used to check whether a decl is part of
        # it already.
        toplevel_decls_set = set()

        def append_decl(decl):
            """
            Append ``decl`` to ``toplevel_decls`` if it's not there yet.
            """
            if decl not in toplevel_decls_set:
                toplevel_decls.append(decl)
                toplevel_decls_set.add(decl)

        # Go through all declarations that appear in the top-level package and
        # organize them in sections the way we want to document them.

        decls = [d.cast(lal.BasicDecl)
                 for d in package_decl.f_public_part.f_decls
                 if d.is_a(lal.BasicDecl)]

        types = {}

        for decl in decls:
            _, annotations = self.get_documentation(decl)

            # Skip documentation for this entity
            if annotations.get('no-document'):
                continue

            if is_documentable_subp(decl):
                # Look for the type under which this subprogram should be
                # documented ("owning_type"). This is either the explicitly
                # asked type ("belongs-to" annotation) or the type that is a
                # primitive for this subprogram (if the type is declared in the
                # same file).
                owning_type = None
                if annotations.get('belongs-to'):
                    owning_type = types[annotations['belongs-to']]
                else:
                    prim_type = decl.f_subp_spec.p_primitive_subp_first_type()
                    if prim_type and prim_type.unit == package_decl.unit:
                        owning_type = prim_type
                        append_decl(owning_type)

                # If we found a relevant type, document the subprogram under
                # it, otherwise document it at the top-level.
                if owning_type:
                    associated_decls[owning_type].append(decl)
                else:
                    append_decl(decl)

            elif decl.is_a(lal.BaseTypeDecl):
                # New type declaration: document it and register it as a type
                types[decl.p_defining_name.text] = decl
                append_decl(decl)

            elif decl.is_a(lal.ObjectDecl):
                # Try to associate object declarations to their type, if there
                # is one in the current package.
                type_name = (decl.f_type_expr.p_designated_type_decl
                             .p_defining_name)
                t = types.get(type_name.text) if type_name else None
                if t:
                    associated_decls[t].append(decl)
                else:
                    append_decl(decl)

            elif decl.is_a(lal.BasicDecl):
                # If decl is not a decl that we explicitly know should be
                # handled the default way, warn.
                if not decl.is_a(lal.ExceptionDecl,
                                 lal.PackageRenamingDecl,
                                 lal.GenericPackageInstantiation,
                                 lal.GenericSubpInstantiation,
                                 lal.GenericPackageDecl,
                                 lal.PackageDecl):
                    self.warn('default entity handling for '
                              f'{P.relpath(decl.unit.filename)}:{decl}')
                append_decl(decl)

        # Get documentation for the top-level package itself
        pkg_doc, annotations = self.get_documentation(package_decl)

        # Create the documentation's content

        # Create a section
        pkg_name = package_decl.p_fully_qualified_name
        self.add_lines([''])

        def handle_decl(decl):
            if decl.is_a(lal.PackageDecl):
                self.handle_package(decl)
            elif decl.is_a(lal.GenericPackageDecl):
                self.handle_package(decl.f_package_decl, gen_package=decl)
            else:
                self.handle_entity(decl)
                with self.indent():
                    for assoc_decls in associated_decls[decl]:
                        self.handle_entity(assoc_decls)

        if self._package_nesting_level == 0:
            self.add_string(
                f"{pkg_name }\n"
                f"{UNDERLINES[self._package_nesting_level] * len(pkg_name )}"
            )
            self.add_lines(['', f".. ada:set_package:: {pkg_name}"])
            # When the package is a thin generic template (the
            # caller passed ``gen_package``), also emit the
            # ``.. ada:generic_package::`` directive for the
            # template itself. The directive consumes only the
            # bare package name; the handler prepends ``generic
            # package `` annotation in the rendered output.
            if gen_package is not None:
                self.add_lines(['', f".. ada:generic_package:: {pkg_name}"])
        else:
            generic = "generic_" if gen_package is not None else ""
            self.add_lines([f".. ada:{generic}package:: {pkg_name}", ""])
            self._indent += 4

        self.add_lines([''] + pkg_doc + [''])

        if gen_package is not None:
            self.add_lines([':Formals:'])

            with self.indent():
                for decl in gen_package.f_formal_part.f_decls:
                    handle_decl(decl)

        # Go through all entities to generate their documentation
        self._package_nesting_level += 1
        for decl in toplevel_decls:
            handle_decl(decl)
        self._package_nesting_level -= 1

        if self._package_nesting_level != 0:
            self._indent -= 4

    def handle_entity(self, decl: lal.BasicDecl):

        def make_profile(s: lal.BaseSubpSpec) -> str:
            """
            Reconstruct a text profile for given subprogram spec, with fully
            qualified type names.
            """

            def typ(te: lal.TypeExpr) -> str:
                # TODO: Anonymous types are not handled fully yet: we just
                # grab their text, but we should expand inner type names too to
                # be fully qualified.
                if te.is_a(lal.AnonymousType):
                    return strip_ws(te.text)
                # Language-defined types (``Integer``, ``Boolean``,
                # ``String``, ``Natural``, ``Positive``, ...) have no
                # ``p_designated_type_decl`` because their declaration
                # lives in the language runtime, not in user source.
                # Fall back to the verbatim source text so the docs
                # still render correctly for functions that take or
                # return language-defined types.
                if te.p_designated_type_decl is None:
                    return strip_ws(te.text)
                else:
                    return te.p_designated_type_decl.p_fully_qualified_name

            params = "({})".format("; ".join(
                f"{strip_ws(p.f_ids.text)} : "
                f"{typ(p.f_type_expr)}"
                for p in s.f_subp_params.f_params
            )) if s.f_subp_params else ""

            returns = (
                f"return {typ(s.f_subp_returns)}" if s.f_subp_returns else ""
            )
            ret = (
                f"{s.f_subp_kind.text} {s.f_subp_name.text}"
                f" {params} {returns}"
            )
            return ret

        # Get the documentation content
        doc, annotations = self.get_documentation(decl)

        def emit_directive(directive_header):
            self.add_lines([directive_header])
            with self.indent():
                self.add_lines([
                    ":package: "
                    f"{decl.p_parent_basic_decl.p_fully_qualified_name}"
                ])

        if is_documentable_subp(decl):
            subp_spec = decl.p_subp_spec_or_null()
            prof = make_profile(subp_spec)
            subp_kind = (
                'procedure' if subp_spec.p_returns is None
                else 'function'
            )
            emit_directive(f".. ada:{subp_kind}:: {prof}")

            for formal in decl.p_subp_spec_or_null().p_abstract_formal_params:
                formal_doc, annots = self.get_documentation(formal)

                # Only generate a param profile if you have doc to show.
                # TODO: This is weird, because params without doc will not be
                # shown. Ideally it would be better to switch on if any param
                # has doc.
                if formal_doc:
                    for i in formal.p_defining_names:
                        fqn = formal.p_formal_type().p_fully_qualified_name
                        self.add_string(f":param {fqn} {i.text}:")
                        with self.indent():
                            self.add_lines(doc)

        elif isinstance(decl, lal.BaseTypeDecl):
            if isinstance(decl, lal.IncompleteTypeDecl):
                return

            prof = f"type {decl.p_relative_name.text}"
            emit_directive(f".. ada:type:: {prof}")

            with self.indent():
                self.add_lines([''])

                # Register components (discriminants and fields)
                comps: Dict[lal.BaseFormalParamDecl,
                            Set[Tuple[lal.DiscriminantValues]]] = {}

                if decl.p_is_access_type():
                    pass
                elif decl.p_is_record_type():
                    try:
                        for shape in decl.p_shapes():
                            for comp in shape.components:
                                ctx = comp.parent.parent.parent
                                s = comps.setdefault(comp, set())
                                if not ctx.is_a(lal.Variant):
                                    s.add(tuple(shape.discriminants_values))
                    except lal.PropertyError:
                        # TODO TA20-019: p_shapes will fail on some types that
                        # are considered records, so we should not crash on
                        # this.
                        pass
                else:
                    # Defensive: libadalang 26.0.0's
                    # `ProtectedTypeDecl.p_discriminants_list` returns
                    # a null access (then wrapped as a libadalang
                    # `PropertyError` with the message
                    # "dereferencing a null access") for protected
                    # types that have no explicit discriminants.
                    # The reproducer is
                    # `vulkan-callback_marshallers.ads:58:5-66:21`,
                    # which defines a parameterless protected
                    # type inside a `private generic` package. laldoc
                    # used to call this property directly; the null
                    # result then crashed the whole laldoc run.
                    # Treat the empty case (None or PropertyError)
                    # as the "no discriminants" case and keep going.
                    # This defensive branch is portable: when the
                    # libadalang bug is fixed upstream and the
                    # property returns an empty list, the
                    # for-loop simply does not execute, which is the
                    # correct behaviour. The reproducer will start
                    # emitting a discriminator list again only when
                    # the libadalang fix lands.
                    try:
                        discriminants = decl.p_discriminants_list()
                    except lal.PropertyError:
                        discriminants = None
                    if discriminants is not None:
                        for comp in discriminants:
                            comps[comp] = set()

                # Emit components
                for comp, discrs in comps.items():
                    inner_doc, annots = self.get_documentation(comp)
                    for dn in comp.p_defining_names:
                        formal_type = comp.p_formal_type()
                        if formal_type.is_a(lal.AnonymousTypeDecl):
                            tn = "``{}``".format(
                                formal_type.text
                            )
                        else:
                            tn = comp.p_formal_type().p_fully_qualified_name
                        comp_kind = (
                            "discriminant" if comp.is_a(lal.DiscriminantSpec)
                            else "component"
                        )
                        self.add_string(f":{comp_kind} {tn} {dn.text}:")
                        with self.indent():
                            self.add_lines(inner_doc)

        elif isinstance(decl, lal.ObjectDecl):
            default_expr = None

            if decl.f_default_expr and annotations.get('document-value', True):
                # If there is a default expression to describe, do it as an
                # additional description. The title will only contain the name
                # up to the type expression.
                default_expr = decl.f_default_expr.text
                last_token = decl.f_type_expr.token_end

            elif decl.f_renaming_clause:
                # If there is a renaming clause, just put everything until the
                # renaming clause in the title.
                last_token = decl.f_renaming_clause.token_end

            else:
                # By default, go until the type expression
                last_token = decl.f_type_expr.token_end

            descr = strip_ws(lal.Token.text_range(
                decl.token_start, last_token
            ))

            emit_directive(f".. ada:object:: {descr}")

            with self.indent():
                self.add_lines([''])
                typ = decl.p_type_expression.p_designated_type_decl
                if typ.is_a(lal.AnonymousTypeDecl):
                    typ_str = f"``{decl.p_type_expression.text}``"
                else:
                    typ_str = typ.p_fully_qualified_name
                if not decl.parent.is_a(lal.GenericFormal):
                    self.add_string(f":objtype: {typ_str}")
                    if default_expr:
                        self.add_string(
                            f":defval: ``{strip_ws(default_expr)}``"
                        )
                    if decl.f_renaming_clause:
                        self.add_string(
                            ":renames: "
                            f"{decl.f_renaming_clause.f_renamed_object.text}"
                        )

        elif isinstance(decl, lal.PackageRenamingDecl):
            name = decl.p_defining_name.text
            renames = decl.p_renamed_package.p_defining_name.text
            emit_directive(f".. ada:package:: {name}")
            with self.indent():
                self.add_lines([''])
                self.add_string(f":renames: {renames}")

        elif isinstance(decl, lal.ExceptionDecl):
            name = decl.p_defining_name.text
            emit_directive(f".. ada:exception:: {name}")

        elif isinstance(decl, lal.GenericPackageDecl):
            # Thin generic template. Emit ``ada:generic_package`` for
            # the template itself; the directive consumes only the
            # bare package name and prepends ``generic package `` in
            # the rendered signature. The body's own declarations are
            # emitted as ordinary nested decls by the upstream walk.
            pkg = decl.f_package_decl
            name = (pkg.f_package_name.text
                    if pkg and pkg.f_package_name
                    else decl.p_defining_name.text)
            emit_directive(f".. ada:generic_package:: {name}")

        elif isinstance(decl, lal.GenericPackageInstantiation):
            sig = strip_ws(lal.Token.text_range(
                decl.token_start, decl.f_generic_pkg_name.token_end
            ))
            emit_directive(f".. ada:generic-package-instantiation:: {sig}")
            with self.indent():
                self.add_lines([''])
                self.add_string(".. code-block:: ada")
                with self.indent():
                    self.add_lines([''] + decl.text.splitlines())
                self.add_lines([''])
                # Resolve the instantiated generic package back to
                # its template. ``p_designated_generic_decl`` is the
                # semantic path but it has two failure modes we work
                # around here:
                #   1. cross-file resolution raises ``PropertyError``
                #      "dereferencing a null access" in libadalang 26.0.0
                #      when the generic lives in a different file with no
                #      body stub on disk.
                #   2. with a GPR-aware unit provider the property
                #      sometimes returns the right node but
                #      ``p_fully_qualified_name`` returns the
                #      instantiation's own name instead of the
                #      generic's name (``f_package_decl.f_package_name``
                #      returns the correct template name in that case).
                # ``f_generic_pkg_name.text`` is syntactic and always
                # correct, so we prefer it.
                if decl.f_generic_pkg_name:
                    generic_fqn = decl.f_generic_pkg_name.text
                else:
                    try:
                        generic_fqn = (
                            decl.p_designated_generic_decl
                            .f_package_decl.f_package_name.text
                        )
                    except (lal.PropertyError, AttributeError):
                        generic_fqn = "?"
                self.add_string(f":instpkg: {generic_fqn}")
        elif isinstance(decl, lal.GenericFormal):
            self.handle_entity(decl.f_decl)
            return
        else:
            print(f"WARNING: Non handled entity: {decl}")

        with self.indent():
            self.add_lines([''] + doc + [''])


if __name__ == '__main__':
    GenerateDoc.run()
