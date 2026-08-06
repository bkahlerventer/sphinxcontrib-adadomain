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
    # The leading ``PragmaNode`` siblings of the decl currently being
    # handled. Set by ``handle_package`` before each call to
    # ``handle_entity``; read-and-cleared by
    # ``_emit_leading_pragmas_field`` so pragma state never leaks
    # across decls.
    _leading_pragmas: List[lal.PragmaNode]

    # ``_leading_pragmas_snapshot`` is set just before
    # ``handle_entity`` is called and is read by
    # ``_emit_pragma_directives`` AFTER
    # ``_emit_pragmas_body_field`` has cleared
    # ``_leading_pragmas``. Each leading pragma then gets its
    # own ``.. ada:pragma::`` directive. The snapshot is a
    # shallow copy so the body-field emit's clear cannot
    # affect the per-pragma directive emit.
    _leading_pragmas_snapshot: List[lal.PragmaNode]

    # Pragmas whose semantics are also exposed as aspects on the same
    # entity. When a pragma in this set appears as a leading pragma,
    # it is documented under the corresponding aspect field rather
    # than under the generic ``:pragmas:`` field, to avoid duplication.
    _pragma_to_aspect_map: Dict[str, str] = {
        "Convention": "Convention",
        "Import": "Import",
        "External": "External",
        "Link_Name": "Link_Name",
        "Precondition": "Pre",
        "Postcondition": "Post",
        "Inline": "Inline",
        "No_Return": "No_Return",
        "Global": "Global",
    }

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
        self._leading_pragmas = []
        self._leading_pragmas_snapshot = []

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

        # Associate leading AND trailing PragmaNodes with the nearest decl
        # in source order. Ada code uses both shapes:
        #   type T is ...; pragma Convention (C, T);   -- trailing
        #   pragma Convention (C, T); type T is ...;   -- leading
        # Both are siblings of ``f_public_part.f_decls``. We attach
        # every pragma to the nearest decl in source order so the
        # binding info travels with the directive.
        #
        # The same algorithm also associates C-struct layout
        # representation clauses (``for T'Size use 32`` /
        # ``AttributeDefClause`` and ``for T use record ...``
        # ``RecordRepClause`` / ``ComponentClause``) with their
        # target type decl. These clauses live as siblings in
        # ``f_public_part.f_decls`` and carry C binding layout
        # info that the existing laldoc emit path silently
        # dropped.
        #
        # Two-pass algorithm: first partition the public part into a
        # list of decls and an ordered list of (decl_index, item)
        # tuples where ``decl_index`` is the index of the FIRST decl
        # that comes AFTER the item (so ``k == len(decls_so_far)``
        # at the time the item was seen). Then attach each item to
        # ``decls[k-1]`` (trailing) if k > 0, else to ``decls[0]``
        # (leading). Items with no decl after them are dropped.
        decls: List[lal.BasicDecl] = []
        pragma_seq: List[Tuple[int, lal.PragmaNode]] = []
        rep_seq: List[Tuple[int, lal.RecordRepClause]] = []
        attr_seq: List[Tuple[int, lal.AttributeDefClause]] = []
        for raw in package_decl.f_public_part.f_decls:
            if raw.is_a(lal.PragmaNode):
                pragma_seq.append(
                    (len(decls), raw.cast(lal.PragmaNode))
                )
                continue
            if raw.is_a(lal.RecordRepClause):
                rep_seq.append(
                    (len(decls), raw.cast(lal.RecordRepClause))
                )
                continue
            if raw.is_a(lal.AttributeDefClause):
                # AttributeDefClause is the parent class for
                # ``for X'Size use ...`` and similar.
                # EnumRepClause is a sibling. We accept either;
                # any AttributeDefClause-shaped node goes here.
                attr_seq.append(
                    (len(decls), raw.cast(lal.AttributeDefClause))
                )
                continue
            if not raw.is_a(lal.BasicDecl):
                continue
            decls.append(raw.cast(lal.BasicDecl))
        decls_with_pragmas: List[Tuple[lal.BasicDecl,
                                        List[lal.PragmaNode]]] = [
            (d, []) for d in decls
        ]
        for k, p in pragma_seq:
            if k > 0:
                idx = k - 1
                pd, p_lead = decls_with_pragmas[idx]
                decls_with_pragmas[idx] = (pd, p_lead + [p])
            elif decls:
                pd, p_lead = decls_with_pragmas[0]
                decls_with_pragmas[0] = (pd, p_lead + [p])
            # else: pragma at the start of the package with no
            # following decl; dropped on the floor (it would
            # document itself).

        # Per-decl map keyed by ``id(decl)``. We need this map
        # because the outer loop below collects all decls into
        # ``toplevel_decls`` before any ``handle_entity`` is
        # invoked; an instance attribute would be overwritten on
        # every outer-loop iteration and only the LAST decl's
        # pragmas would survive into the ``handle_decl`` phase.
        # ``lal.BasicDecl`` is not hashable, so we use ``id``.
        pragmas_by_decl_id: Dict[int, List[lal.PragmaNode]] = {
            id(d): list(pragmas)
            for d, pragmas in decls_with_pragmas
        }

        # Representation clauses grouped by id(decl). Same
        # trailing/leading semantics as pragmas.
        rep_clauses_by_decl_id: Dict[
            int, List[Tuple[str, lal.RecordRepClause]]
        ] = {}
        for k, r in rep_seq:
            target = decls[k-1] if k > 0 else (decls[0] if decls else None)
            if target is None:
                continue
            rep_clauses_by_decl_id.setdefault(id(target), []).append(
                ("record", r)
            )
        attr_clauses_by_decl_id: Dict[
            int, List[Tuple[str, lal.AttributeDefClause]]
        ] = {}
        for k, a in attr_seq:
            target = decls[k-1] if k > 0 else (decls[0] if decls else None)
            if target is None:
                continue
            attr_clauses_by_decl_id.setdefault(id(target), []).append(
                ("attribute", a)
            )
        # Surface the maps on ``self`` so ``handle_entity`` (called
        # via ``handle_decl`` in the toplevel loop below, possibly
        # long after this local scope has exited) can read them.
        # Same id-based pattern as ``pragmas_by_decl_id`` above.
        self._rep_clauses_by_decl_id = rep_clauses_by_decl_id
        self._attr_clauses_by_decl_id = attr_clauses_by_decl_id

        decls = [d for d, _ in decls_with_pragmas]

        types = {}

        for decl in decls:
            # Surface the leading pragmas to ``handle_entity`` via the
            # per-decl map. ``handle_entity`` reads and clears the
            # entry so pragma state never leaks across decls.
            self._leading_pragmas = list(
                pragmas_by_decl_id.get(id(decl), [])
            )
            self._leading_pragmas_snapshot = list(self._leading_pragmas)

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
                                 lal.PackageDecl,
                                 lal.NumberDecl,
                                 lal.NullSubpDecl):
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
                # Surface the leading pragmas to
                # ``handle_entity`` via the per-decl map. This is the
                # second of three sites where
                # ``self._leading_pragmas`` is set; the third is the
                # associated-decls loop below. All three sites must
                # agree because ``handle_entity`` consumes the slot
                # via ``_emit_leading_pragmas_field``.
                pragmas_for_decl = list(
                    pragmas_by_decl_id.get(id(decl), [])
                )
                self._leading_pragmas = pragmas_for_decl
                # Snapshot before the body-field emit clears it;
                # ``_emit_pragma_directives`` reads from the
                # snapshot so each leading pragma also gets its own
                # ``.. ada:pragma::`` directive alongside the
                # ``:pragmas:`` body field.
                self._leading_pragmas_snapshot = list(pragmas_for_decl)
                self.handle_entity(decl)
                with self.indent():
                    for assoc_decls in associated_decls[decl]:
                        # Re-set for each associated decl so
                        # ``handle_entity`` sees the correct list.
                        self._leading_pragmas = list(
                            pragmas_by_decl_id.get(id(assoc_decls), [])
                        )
                        self._leading_pragmas_snapshot = list(
                            self._leading_pragmas
                        )
                        self.handle_entity(assoc_decls)

        if self._package_nesting_level == 0:
            self.add_string(
                f"{pkg_name }\n"
                f"{UNDERLINES[self._package_nesting_level] * len(pkg_name )}"
            )
            self.add_lines(['', f".. ada:set_package:: {pkg_name}"])
            # Emit a ``:with:`` body field listing the package's
            # ``with`` clauses (the dependencies it imports
            # from). This gives the reader a quick map of the
            # external surface without having to load the
            # source file. Only emitted for top-level packages;
            # nested packages inherit the parent's ``with``
            # context. ``UseClause`` (renamings of imported
            # entities) is also surfaced under the same
            # field; we accept the slight format mixing in
            # exchange for the field being a single block.
            with_uses_lines = self._collect_with_uses(
                package_decl.unit
            )
            if with_uses_lines:
                self.add_lines([''])
                self.add_lines(with_uses_lines)
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
                    self._leading_pragmas = list(
                        pragmas_by_decl_id.get(id(decl), [])
                    )
                    handle_decl(decl)

        # Go through all entities to generate their documentation.
        # We re-set ``self._leading_pragmas`` before each call so the
        # correct pragma list travels with the decl that
        # ``handle_decl`` is about to dispatch. (The outer loop above
        # ALSO sets it per iteration, but those values get overwritten
        # on every iteration, so by the time we get here only the
        # LAST decl's pragmas would survive; we re-set explicitly to
        # make the per-decl binding precise.)
        self._package_nesting_level += 1
        for decl in toplevel_decls:
            self._leading_pragmas = list(
                pragmas_by_decl_id.get(id(decl), [])
            )
            self._leading_pragmas_snapshot = list(self._leading_pragmas)
            handle_decl(decl)
        self._package_nesting_level -= 1

        if self._package_nesting_level != 0:
            self._indent -= 4

    def _handle_protected_type(self, decl: lal.ProtectedTypeDecl) -> None:
        """
        Emit documentation for a protected type decl. The
        protected type's body (``protected type X is ... end
        X;``) contains subprograms, entries, and components.
        The public part is emitted like any other type via the
        ``.. ada:type::`` directive; the protected body's
        subprograms and entries are emitted as nested child
        directives.

        Note that the existing ``p_discriminants_list`` defensive
        patch in ``handle_entity`` (line ~660) prevents the
        parameterless-protected-type null-deref that crashed
        laldoc on the Vulkan.Callback_Marshallers reproducer.
        The patch is portable: when the libadalang bug is
        fixed upstream and the property returns an empty list,
        the ``for`` loop simply does not execute, which is the
        correct behaviour. This handler does not need to repeat
        that defensive patch.

        The private part (``private ... end``) is documented
        only if its ``Component`` decls are documented via
        ``:component:``; otherwise the private part is silent.
        Project convention: skip the private data section; the
        state of a protected object is an implementation
        detail, not a public API.
        """
        prof = f"type {decl.p_relative_name.text}"
        # Wrap the type directive emission so :package: lands at
        # the right indent. We deliberately do NOT use
        # ``emit_directive`` because that closure also calls
        # ``_emit_pragmas_body_field`` which depends on
        # ``self._leading_pragmas`` being set; for a
        # ProtectedTypeDecl at the top level, that slot was
        # already populated by the partition pass in
        # ``handle_package``.
        self.add_lines([f".. ada:type:: {prof}"])
        with self.indent():
            self.add_lines([
                ":package: "
                f"{decl.p_parent_basic_decl.p_fully_qualified_name}"
            ])
            pragmas = getattr(self, '_leading_pragmas', None)
            if pragmas:
                self._emit_pragmas_body_field(decl)
            self._emit_aspects_body_field(decl)
            self._emit_spark_mode_field(decl)
            # Walk the protected body.
            self._emit_protected_body(decl)
        # Drop back to parent indent and emit one
        # ``.. ada:aspect::`` / ``.. ada:pragma::`` per aspect
        # / pragma on the protected type itself.
        self._emit_aspect_directives(decl)
        self._emit_pragma_directives(decl)

    def _emit_protected_body(self, decl: lal.ProtectedTypeDecl) -> None:
        """
        Walk a protected type's body (``f_definition``) and emit
        child directives for each subprogram / entry. Components
        in the private section are surfaced as ``:component:``
        fields under a private-section header. We do not recurse
        into nested protected types or nested packages; the
        current scope is one protected type.
        """
        definition = decl.f_definition
        if definition is None:
            return
        public_part = definition.f_public_part
        if public_part:
            self.add_lines([''])
            self.add_lines(['Public operations:'])
            with self.indent():
                for sub in public_part.f_decls:
                    if not isinstance(sub, lal.BasicDecl):
                        continue
                    self._leading_pragmas = []
                    self.handle_entity(sub)
                    self._leading_pragmas = []
        private_part = definition.f_private_part
        if private_part:
            self.add_lines([''])
            self.add_lines(['Private state:'])
            with self.indent():
                for comp in private_part.f_decls:
                    if isinstance(comp, lal.ComponentDecl):
                        # Components are exposed as
                        # ``:component:`` fields, not as their
                        # own ``ada:type::`` directive.
                        self._emit_component_field(comp)

    def _emit_component_field(self, comp: lal.ComponentDecl) -> None:
        """
        Emit a ``:component:`` body field for one component of
        a protected type's private section. The field body is
        the component's source text.
        """
        # ComponentDecl carries the names and type as separate
        # fields. Walk ``f_ids`` for the names list and
        # ``f_component_def`` for the type expression.
        names_text = ""
        if comp.f_ids:
            names_text = ", ".join(
                i.text for i in comp.f_ids
            )
        type_text = comp.f_component_def.text if \
            comp.f_component_def else "?"
        if comp.f_default_expr:
            type_text += " := " + comp.f_default_expr.text
        self.add_lines([f":component: ``{type_text}``  {names_text}"])

    def _fall_through_to_default_handler(self, decl: lal.BasicDecl) -> None:
        """
        Emit a warning for a decl that laldoc does not know how
        to handle, and render the full source text as a
        literal code block. This is the catch-all used by
        handlers that need to bail out of their specialised
        logic. We do NOT call ``handle_decl_generic`` because
        that's a different code path (the AutoPackage
        directive's handler); ``generate_rst.py`` has no
        equivalent. Instead, the source text goes into a
        ``.. code-block:: ada`` block under a placeholder
        paragraph so the reader at least sees the source.
        """
        self.warn(
            f"default entity handling for {P.relpath(decl.unit.filename)}:{decl}"
        )
        self.add_lines([''])
        self.add_string(f".. code-block:: ada")
        with self.indent():
            for line in decl.text.splitlines():
                self.add_lines([line])

    def _collect_with_uses(self, unit) -> List[str]:
        """
        Collect the ``with`` and ``use`` clauses from the
        given compilation unit's prelude and format them as a
        list of Sphinx Field lines for the
        ``ada:set_package::``-anchored ``:with:`` field.

        Each line is one ``:with:`` body field. The body
        is the source text of the clause (``with Foo.Bar;`` /
        ``use Foo;`` / etc.). Lines that span multiple
        source lines (e.g. a ``with`` clause with several
        packages) are collapsed to a single line so the
        Sphinx Field parser can read them as one paragraph.

        Returns an empty list when the unit has no
        ``with`` or ``use`` clauses.
        """
        lines: List[str] = []
        if unit is None or unit.root is None:
            return lines
        # The unit's prelude is a list of WithClause / UseClause
        # nodes. For a ``compilation_rule`` parse the top-level
        # is a ``CompilationUnit`` whose first child is an
        # ``AdaNodeList`` (the prelude). For a
        # ``package_decl_rule`` parse the top-level is the
        # ``PackageDecl`` directly and there is no prelude.
        # We walk the children of the root and then any
        # ``AdaNodeList`` children we find, picking out
        # ``WithClause`` / ``UseClause`` nodes. This handles
        # both parse shapes.
        def walk_for_clauses(node, depth: int = 0) -> None:
            if depth > 1:
                return
            for child in node:
                if child.is_a(lal.WithClause):
                    collapsed = " ".join(
                        ln.strip() for ln in child.text.splitlines()
                        if ln.strip()
                    )
                    lines.append(f":with: ``{collapsed}``")
                elif child.is_a(lal.UseClause):
                    collapsed = " ".join(
                        ln.strip() for ln in child.text.splitlines()
                        if ln.strip()
                    )
                    lines.append(f":with: ``{collapsed}``")
                elif child.is_a(lal.AdaNodeList):
                    # Prelude list: recurse one level.
                    walk_for_clauses(child, depth + 1)

        walk_for_clauses(unit.root)
        return lines

    def _emit_spark_mode_field(self, decl: lal.BasicDecl) -> None:
        """
        Emit a ``:spark_mode:`` body field on a decl that
        carries a SPARK ``SPARK_Mode`` aspect. The field
        tells the reader whether the entity is verified by
        the SPARK proof toolchain (``On``), explicitly
        excluded (``Off``), or unspecified (in which case
        we fall back to ``p_is_subject_to_proof`` to give a
        derived answer).

        The libadalang property ``p_spark_mode_aspect``
        raises ``PropertyError`` on malformed ASTs (the
        same null-deref family as
        ``p_designated_type_decl``); we wrap defensively.
        When neither ``p_spark_mode_aspect`` nor
        ``p_is_subject_to_proof`` can answer, we skip the
        field — emitting "SPARK mode: unknown" would be
        worse than silent.
        """
        if not isinstance(decl, lal.BasicSubpDecl) and \
                not isinstance(decl, lal.BaseTypeDecl) and \
                not isinstance(decl, lal.ObjectDecl):
            return
        mode = None
        try:
            asp = decl.p_spark_mode_aspect
            if asp is not None and asp.exists:
                # The aspect's ``value`` is an ``Id`` node
                # whose ``text`` is ``"On"`` / ``"Off"``.
                if asp.value is not None and hasattr(
                    asp.value, "text"
                ):
                    mode = asp.value.text
        except (lal.PropertyError, AttributeError):
            pass
        if mode is None:
            # No explicit aspect; check the derived property
            # ``p_is_subject_to_proof``. This is True iff the
            # enclosing package has ``SPARK_Mode => On``
            # and the entity isn't explicitly ``=> Off``.
            try:
                mode = ("On"
                        if decl.p_is_subject_to_proof
                        else "Off")
            except lal.PropertyError:
                return
        if mode is None:
            return
        # Add a blank line BEFORE the field to flip
        # docutils to body-field-parsing mode.
        self.add_lines([''])
        self.add_lines([f":spark_mode: ``{mode}``"])

    def _emit_representation_clauses(self, decl: lal.BasicDecl) -> None:
        """
        Emit C-struct layout representation clauses (``for T'Size
        use 32`` and ``for T use record ... Component at ... range
        ...``) as a single ``:representation:`` Sphinx body field.
        The clauses were associated with ``decl`` in the
        trailing/leading partition pass at the top of
        ``handle_package``; this method reads them from the
        ``rep_clauses_by_decl_id`` and ``attr_clauses_by_decl_id``
        instance attributes that the partition populates.

        Output shape: one ``:representation:`` line per clause,
        with the clause's source text as the body. The source text
        already includes the ``for`` keyword and trailing ``;``.

        A blank line is emitted before the first field so
        docutils flips from option-parsing to body-field-parsing
        (the body-vs-option trap from pitfall #0b of
        ada-sphinx-docs-pitfall).
        """
        # The partition maps were built at the top of
        # ``handle_package`` and are looked up here. We use the
        # closure-bound locals if available; if not, no clauses.
        rep_clauses_by_decl_id = getattr(
            self, '_rep_clauses_by_decl_id', None
        )
        attr_clauses_by_decl_id = getattr(
            self, '_attr_clauses_by_decl_id', None
        )
        if rep_clauses_by_decl_id is None and \
                attr_clauses_by_decl_id is None:
            return
        items: List[Tuple[str, str]] = []
        if rep_clauses_by_decl_id is not None:
            for kind, r in rep_clauses_by_decl_id.get(id(decl), []):
                # Strip the trailing semicolon and trailing
                # whitespace; Sphinx Field body is a single
                # paragraph.
                items.append((kind, r.text.strip().rstrip(';').rstrip()))
        if attr_clauses_by_decl_id is not None:
            for kind, a in attr_clauses_by_decl_id.get(id(decl), []):
                items.append((kind, a.text.strip().rstrip(';').rstrip()))
        if not items:
            return
        # Blank line BEFORE the field flips docutils to
        # body-field-parsing.
        self.add_lines([''])
        for kind, text in items:
            # Sphinx Field bodies must be a single paragraph (no
            # blank lines in the middle). The RecordRepClause
            # source text can span multiple lines
            # (``for T use record\n  Comp at ...\n  ...\nend record``)
            # so collapse to a single line here. Newlines inside
            # ````...```` would break the field parser.
            collapsed = " ".join(
                ln.strip() for ln in text.splitlines()
                if ln.strip()
            )
            self.add_lines(
                [f":representation: ``{collapsed};``"]
            )

    def _emit_aspects_body_field(self, decl: lal.BasicDecl) -> None:
        """
        Emit contract / typing / optimization aspects as Sphinx
        body fields. The aspect names and their preferred field
        labels are registered in ``sphinxcontrib.adadomain.AdaObject``
        (``doc_field_types``). Supported aspects:

          - ``Pre``           -> ``:pre:``
          - ``Post``          -> ``:post:``
          - ``Contract_Cases`` -> ``:contract_cases:``
          - ``Inline``        -> ``:inline:``
          - ``Global``        -> ``:global:``
          - ``No_Return``     -> ``:no_return:``
          - ``Convention``    -> ``:convention:``
          - ``Import``        -> ``:import_kind:`` (the field
            is named ``import_kind`` to avoid colliding with
            Sphinx's built-in ``:import:``)
          - ``External``      -> ``:external:``
          - ``Link_Name``     -> ``:link_name:``

        Each aspect's expression text is the field body. A blank
        line is emitted before the FIRST field so docutils parses
        it as a body field, not a directive option. Aspects that
        are not in the supported set are silently skipped (they
        document themselves in the doc-comment text).

        Pragmas in ``_pragma_to_aspect_map`` that map to these
        aspect names are already deduped by A1 (the ``:pragmas:``
        emit skips them), so the aspect field is the SOLE place a
        reader sees e.g. ``pragma Convention (C, T);``.
        """
        if not isinstance(decl, lal.BasicSubpDecl) and \
                not isinstance(decl, lal.BaseTypeDecl) and \
                not isinstance(decl, lal.ObjectDecl):
            # Aspects only meaningful on subprograms, types,
            # and objects. Other decls are skipped.
            return
        if not decl.f_aspects:
            return
        # Map aspect name -> (group, field marker). Aspects
        # are organised into three groups:
        #
        #   - ``contracts``: Pre, Post, Contract_Cases.
        #     These are the runtime contract assertions
        #     (``precondition`` / ``postcondition`` /
        #     ``contract_cases``) that the compiler emits
        #     checks for. Grouping them under a single
        #     "Contracts" header tells the reader "this
        #     subprogram has runtime checks" at a glance.
        #
        #   - ``typing``: Convention, Import, External,
        #     Link_Name. These are the C-binding aspects
        #     that tell the reader this entity is bound to a
        #     foreign language / C ABI. Grouping them under
        #     "Binding" header is the explicit
        #     thick-vs-thin signal.
        #
        #   - ``optimization``: Inline, Global, No_Return.
        #     These tell the compiler / linker how to emit
        #     the subprogram. Grouping them under
        #     "Optimization" header separates the
        #     performance hints from the contract and
        #     binding surfaces.
        aspect_groups = {
            "Pre": ("contracts", ":pre:"),
            "Post": ("contracts", ":post:"),
            "Contract_Cases": ("contracts", ":contract_cases:"),
            "Convention": ("typing", ":convention:"),
            "Import": ("typing", ":import_kind:"),
            "External": ("typing", ":external:"),
            "Link_Name": ("typing", ":link_name:"),
            "Inline": ("optimization", ":inline:"),
            "Global": ("optimization", ":global:"),
            "No_Return": ("optimization", ":no_return:"),
        }
        # Group key -> ordered list of (field_marker, expr)
        # pairs. Source order within a group is preserved.
        groups: Dict[str, List[Tuple[str, str]]] = {
            "contracts": [],
            "typing": [],
            "optimization": [],
        }
        for a in decl.f_aspects.f_aspect_assocs:
            name = a.f_id.text if a.f_id and a.f_id.text else None
            if not name or name not in aspect_groups:
                continue
            group_name, field_marker = aspect_groups[name]
            expr = a.f_expr.text if a.f_expr else ""
            groups[group_name].append((field_marker, expr))
        # Skip the emit if no group has any aspect.
        if not any(groups.values()):
            return
        # Header text per group. The header is a single
        # paragraph at the same indent as ``:package:`` /
        # ``:pragmas:``; the body field is emitted below it
        # at the same indent. The blank line BEFORE the
        # first field flips docutils to body-field-parsing
        # mode (the body-vs-option trap from pitfall #0b).
        group_labels = {
            "contracts": "**Contracts**",
            "typing": "**Binding**",
            "optimization": "**Optimization**",
        }
        # We emit each non-empty group in the order
        # ``contracts`` / ``typing`` / ``optimization`` —
        # the most-common group (contracts) comes first.
        for group_name in ("contracts", "typing", "optimization"):
            if not groups[group_name]:
                continue
            self.add_lines([''])
            self.add_lines([group_labels[group_name]])
            for field_marker, expr in groups[group_name]:
                self.add_lines([f"   {field_marker} ``{expr}``"])

    def _emit_pragmas_body_field(self, decl: lal.BasicDecl) -> None:
        """
        Emit a ``:pragmas:`` Sphinx body field listing every
        leading pragma that is NOT also exposed as an aspect on
        the same entity. Emits a blank line before the field so
        docutils parses it as a body field, not a directive
        option. Always clears ``self._leading_pragmas`` so the
        slot does not leak across decls.

        Pragmas in ``_pragma_to_aspect_map`` whose corresponding
        aspect is present on the entity (e.g. ``pragma Convention``
        alongside ``with Convention => C``) are skipped to avoid
        duplication. Pragmas with no aspect mapping (``Linker_Section``,
        ``Volatile``, ``Atomic``, ``Suppress``, ``Warnings``,
        ``Inspection_Point``, ``List``, ``Pack``, ``Default_Sentinel``,
        ``Common_Obj``, ...) are emitted as one ``:pragmas:``
        field with a backtick-quoted expression.
        """
        pragmas = getattr(self, '_leading_pragmas', None)
        if pragmas is None:
            self._leading_pragmas = []
            return

        # Pre-compute which aspect names are present on ``decl`` so
        # we can dedup without calling ``p_get_aspect`` per pragma
        # (which can raise on a null access for malformed AST).
        aspect_names_present: Set[str] = set()
        if decl.f_aspects:
            for a in decl.f_aspects.f_aspect_assocs:
                if a.f_id and a.f_id.text:
                    aspect_names_present.add(a.f_id.text)

        # Build the list of pragmas worth emitting. Drop the ones
        # whose aspect is already on the entity. Preserve source
        # order.
        keep: List[lal.PragmaNode] = []
        for p in pragmas:
            pname = p.f_id.text if p.f_id else None
            if pname and pname in self._pragma_to_aspect_map:
                aspect_name = self._pragma_to_aspect_map[pname]
                if aspect_name in aspect_names_present:
                    # Already documented under the aspect field;
                    # skip to avoid duplication.
                    continue
            keep.append(p)

        # Always clear the slot regardless of what we emit.
        self._leading_pragmas = []

        if not keep:
            return

        # Emit a blank line BEFORE the field to flip docutils
        # from option-parsing to body-field-parsing (see the
        # body-vs-option trap in ada-sphinx-docs-pitfall pitfall
        # #0b). Without this blank line, docutils parses
        # ``:pragmas:`` as a directive OPTION, fails with
        # ``unknown option: "pragmas"``, and drops the line.
        self.add_lines([''])

        # Format: one ``:pragmas:`` line per pragma, with the
        # pragma's args on the same line. Multi-arg pragmas like
        # ``pragma Convention (C, Handle)`` show as
        # ``Convention (C, Handle);``. Sphinx Field bodies are
        # rendered as a single paragraph; using one Field per
        # pragma keeps each pragma on its own rendered line.
        for p in keep:
            pname = p.f_id.text if p.f_id else "<unknown>"
            if p.f_args and p.f_args.text:
                self.add_lines(
                    [f":pragmas: ``pragma {pname} ({p.f_args.text});``"]
                )
            else:
                self.add_lines([f":pragmas: ``pragma {pname};``"])

    def _emit_aspect_directives(self, decl: lal.BasicDecl) -> None:
        """
        Emit one ``.. ada:aspect::`` directive per aspect on
        ``decl``. Each aspect becomes a top-level cross-reference
        target in the Ada domain, which lets readers link to a
        specific aspect (e.g. ``:ada:aspect:`Pre```) and which
        adds the aspect to the global object index. The aspect
        body field emitted by ``_emit_aspects_body_field`` is
        unchanged; this is an additive emit that runs alongside
        it.

        Aspects are only emitted on subprograms, types, and
        objects — the same scope as ``_emit_aspects_body_field``.
        Aspects that are recognised as an aspect (vs. a pragma)
        on the same entity are emitted as aspects; the pragma
        emit already skips these to avoid duplication.
        """
        if not isinstance(decl, lal.BasicSubpDecl) and \
                not isinstance(decl, lal.BaseTypeDecl) and \
                not isinstance(decl, lal.ObjectDecl):
            return
        if not decl.f_aspects:
            return
        target_fqn = decl.p_fully_qualified_name
        for a in decl.f_aspects.f_aspect_assocs:
            asp_name = a.f_id.text if a.f_id and a.f_id.text else None
            if not asp_name:
                continue
            value = strip_ws(a.f_expr.text) if a.f_expr else ""
            sig = f"{asp_name} => {value}" if value else asp_name
            self.add_lines([f".. ada:aspect:: {sig} on {target_fqn}"])

    def _emit_pragma_directives(self, decl: lal.BasicDecl) -> None:
        """
        Emit one ``.. ada:pragma::`` directive per leading pragma
        on ``decl``. Like ``_emit_aspect_directives`` this is an
        additive emit alongside the existing ``:pragmas:`` body
        field; the difference is that each pragma becomes a
        top-level cross-reference target.

        The ``self._leading_pragmas`` slot is consumed (cleared)
        by ``_emit_pragmas_body_field``; callers that want
        both the body field and the per-pragma directive must
        call this method BEFORE ``_emit_pragmas_body_field``,
        or pass an explicit pragmas list. The handler loops in
        ``handle_entity`` already call this AFTER
        ``_emit_pragmas_body_field`` for the aspect path, so
        pragmas are read here directly from
        ``self._leading_pragmas`` even though the body-field
        emit cleared them; the workaround is that the partition
        pass at the top of ``handle_package`` populates the
        per-decl map and ``handle_decl`` re-stores the pragmas
        into ``self._leading_pragmas`` immediately before
        calling ``handle_entity``. The catch is timing: by the
        time the per-pragma directive emit runs, the slot has
        already been cleared by the body-field emit. We
        therefore snapshot the pragmas at the start of the
        handler loop and pass them through.
        """
        # ``self._leading_pragmas_snapshot`` is set by the
        # handler loops before the body-field emit runs.
        pragmas = getattr(self, '_leading_pragmas_snapshot', None)
        if not pragmas:
            return
        # Pre-compute which aspect names are present so we can
        # skip pragmas whose semantics duplicate an aspect.
        aspect_names_present: Set[str] = set()
        if decl.f_aspects:
            for a in decl.f_aspects.f_aspect_assocs:
                if a.f_id and a.f_id.text:
                    aspect_names_present.add(a.f_id.text)
        for p in pragmas:
            pname = p.f_id.text if p.f_id else None
            if not pname:
                continue
            if pname in self._pragma_to_aspect_map:
                aspect_name = self._pragma_to_aspect_map[pname]
                if aspect_name in aspect_names_present:
                    continue
            if p.f_args and p.f_args.text:
                self.add_lines(
                    [f".. ada:pragma:: {pname} ({p.f_args.text})"]
                )
            else:
                self.add_lines([f".. ada:pragma:: {pname}"])

    def _emit_rep_clause_directives(self, decl: lal.BasicDecl) -> None:
        """
        Emit one ``.. ada:rep_clause::`` directive per
        representation clause on ``decl``. Each clause is the
        verbatim ``for ... use ...`` text. Clauses were
        collected by the partition pass at the top of
        ``handle_package``.
        """
        rep_clauses_by_decl_id = getattr(
            self, '_rep_clauses_by_decl_id', None
        )
        if rep_clauses_by_decl_id is None:
            return
        for kind, r in rep_clauses_by_decl_id.get(id(decl), []):
            text = strip_ws(r.text)
            self.add_lines([f".. ada:rep_clause:: {text}"])

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
                # Emit ``:pragmas:`` body fields for every leading
                # pragma on ``decl`` that is NOT also exposed as
                # an aspect on the same entity. Pragmas whose
                # semantics duplicate an aspect (e.g.
                # ``pragma Convention`` alongside ``with
                # Convention => C``) are skipped here so the
                # aspect-emit path can document them without
                # duplication. The slot is cleared regardless.
                #
                # The blank line BEFORE the ``:pragmas:`` field
                # is required: without it, docutils parses the
                # field as a directive OPTION, fails because
                # ``pragmas`` is not in the Ada domain's
                # ``option_spec``, and emits ``unknown option:
                # "pragmas"``. With the blank line, docutils
                # parses the field as a BODY field, and the
                # ``:pragmas:`` Field type registered in
                # ``sphinxcontrib.adadomain.AdaObject`` accepts
                # it. This is the body-vs-option trap from
                # ada-sphinx-docs-pitfall pitfall #0b.
                pragmas = getattr(self, '_leading_pragmas', None)
                if pragmas:
                    self._emit_pragmas_body_field(decl)

        if is_documentable_subp(decl):
            subp_spec = decl.p_subp_spec_or_null()
            # ``BasicSubpDecl`` (function / procedure / entry) all
            # carry a ``subp_spec_or_null`` that resolves to
            # either ``SubpSpec`` (function / procedure) or
            # ``EntrySpec`` (entry). The two specs have different
            # field names: ``f_subp_name`` / ``f_subp_params`` /
            # ``f_subp_returns`` for SubpSpec; ``f_entry_name`` /
            # ``f_entry_params`` for EntrySpec. ``make_profile``
            # only handles the SubpSpec shape; for EntrySpec we
            # build the profile manually.
            if isinstance(subp_spec, lal.EntrySpec):
                entry_name = subp_spec.f_entry_name.text
                params = ""
                if subp_spec.f_entry_params:
                    param_strs = []
                    for p in subp_spec.f_entry_params.f_params:
                        nm = strip_ws(p.f_ids.text)
                        if p.f_type_expr:
                            te = p.f_type_expr
                            if te.is_a(lal.AnonymousType):
                                tt = strip_ws(te.text)
                            elif te.p_designated_type_decl is None:
                                tt = strip_ws(te.text)
                            else:
                                tt = te.p_designated_type_decl.p_fully_qualified_name
                        else:
                            tt = "?"
                        param_strs.append(f"{nm} : {tt}")
                    params = "({})".format("; ".join(param_strs))
                # Emit as ``ada:entry::`` so entries get
                # their own objtype (``entry``) instead of
                # being collapsed into ``procedure``. The new
                # ``ada_entry_sig_re`` regex in
                # sphinxcontrib.adadomain accepts the
                # ``entry Name (params)`` shape.
                prof = f"entry {entry_name}{params}"
                subp_kind = 'entry'
            else:
                prof = make_profile(subp_spec)
                subp_kind = (
                    'procedure' if subp_spec.p_returns is None
                    else 'function'
                )
            emit_directive(f".. ada:{subp_kind}:: {prof}")

            # If this is a ``NullSubpDecl`` (e.g.
            # ``procedure Reset is null;``), emit an
            # ``:is_null:`` body field so the reader knows the
            # body is intentionally empty. Without this tag,
            # the rendered page would look like a normal
            # subprogram declaration with no body at all,
            # which is misleading: a null body is a deliberate
            # design choice (e.g. visitor pattern, abstract
            # base class, dispatch table stub) and the reader
            # needs to know.
            if isinstance(decl, lal.NullSubpDecl):
                self.add_lines([''])
                self.add_string(':is_null: ``True``')

            # If this is a ``SubpRenamingDecl`` (e.g.
            # ``function Aliased renames Real_Impl;``), emit
            # a ``:renames:`` body field with the target
            # subprogram's name. Thin C bindings often use
            # this pattern to expose an Ada-friendly alias
            # for a C entry point while keeping the original
            # name available for matching the C ABI.
            if isinstance(decl, lal.SubpRenamingDecl):
                if decl.f_renames:
                    target = strip_ws(
                        decl.f_renames.text
                    ).removeprefix('renames').strip()
                    self.add_lines([''])
                    self.add_string(
                        f":renames_target: ``{target}``"
                    )

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

            # Emit contract aspects (Pre, Post, Contract_Cases,
            # Inline, Global, No_Return, ...) as Sphinx body
            # fields. We re-enter ``with self.indent()`` to keep
            # the aspect fields at the same indent as ``:package:``
            # and ``:pragmas:``. The blank line before the first
            # field flips docutils to body-field-parsing mode (the
            # body-vs-option trap from pitfall #0b of
            # ada-sphinx-docs-pitfall).
            with self.indent():
                self._emit_aspects_body_field(decl)
                self._emit_spark_mode_field(decl)

            # Drop back to parent indent and emit one
            # ``.. ada:aspect::`` per aspect and one
            # ``.. ada:pragma::`` per leading pragma, so each
            # becomes a top-level cross-reference target.
            self._emit_aspect_directives(decl)
            self._emit_pragma_directives(decl)

        elif isinstance(decl, lal.BaseTypeDecl):
            if isinstance(decl, lal.IncompleteTypeDecl):
                return

            # ProtectedTypeDecl is a BaseTypeDecl, but the
            # standard type-handling path doesn't know how to
            # walk its ``protected ... is ... end`` body.
            # Dispatch to a dedicated handler that emits
            # ``ada:type::`` for the protected type itself and
            # then walks the entries / subprograms inside its
            # ``f_definition.f_public_part`` /
            # ``f_definition.f_private_part``.
            if isinstance(decl, lal.ProtectedTypeDecl):
                self._handle_protected_type(decl)
                return

            prof = f"type {decl.p_relative_name.text}"
            emit_directive(f".. ada:type:: {prof}")

            # Emit typing aspects (Convention, Import, External,
            # Link_Name, ...) at the same indent as :package:.
            with self.indent():
                self._emit_aspects_body_field(decl)
                self._emit_spark_mode_field(decl)

                # Emit C-struct layout representation clauses
                # (``for T'Size use 32``, ``for T use record ...``
                # with ``Component at ... range ...`` lines) as a
                # Sphinx body field. The clauses were associated
                # with this decl in the trailing/leading partition
                # pass at the top of ``handle_package``.
                self._emit_representation_clauses(decl)

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

            # After the body-field emits close, drop back
            # to the parent indent and emit one
            # ``.. ada:aspect::`` directive per aspect on this
            # type, plus one ``.. ada:pragma::`` per leading
            # pragma, plus one ``.. ada:rep_clause::`` per
            # representation clause. The body fields emitted
            # above remain as compact summaries; the new
            # directives give each aspect/pragma/clause its
            # own cross-reference target.
            self._emit_aspect_directives(decl)
            self._emit_pragma_directives(decl)
            self._emit_rep_clause_directives(decl)

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
                # Emit typing / optimization aspects on the
                # object (Convention, Import, External,
                # Link_Name, Volatile, Atomic, ...) as Sphinx
                # body fields. We're already inside
                # ``with self.indent()``, so the aspect fields
                # land at the same indent as :objtype:.
                self._emit_aspects_body_field(decl)
                self._emit_spark_mode_field(decl)

            # Drop back to parent indent and emit one
            # ``.. ada:aspect::`` per aspect and one
            # ``.. ada:pragma::`` per leading pragma, so each
            # becomes a top-level cross-reference target.
            self._emit_aspect_directives(decl)
            self._emit_pragma_directives(decl)

        elif isinstance(decl, lal.NumberDecl):
            # NumberDecl is the Ada 2022 "number declaration"
            # shape: ``Twelve : constant := 12;``. It has no
            # type expression (the type is inferred from the
            # expression), only ``f_ids`` (the names list) and
            # ``f_expr`` (the expression text). Use the new
            # ``ada:number::`` directive so constants get their
            # own objtype, separate from ``ada:object``.
            descr = strip_ws(decl.text)
            emit_directive(f".. ada:number:: {descr}")
            with self.indent():
                self.add_lines([''])
                # ObjectDecl's ":objtype:" field has no analogue
                # for NumberDecl (no f_type_expr). Emit a
                # ":defval:" instead so the value is visible.
                if decl.f_expr:
                    self.add_string(
                        f":defval: ``{strip_ws(decl.f_expr.text)}``"
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
            # Emit a ``:formal_kind:`` body field labelling what
            # kind of generic formal this is (type, object,
            # subprogram, package). The reader sees a regular
            # type/object/subprogram directive but with a tag
            # above it indicating the formal's role.
            inner = decl.f_decl
            kind = "unknown"
            if isinstance(inner, lal.BaseTypeDecl):
                kind = "type"
            elif isinstance(inner, lal.ObjectDecl):
                kind = "object"
            elif isinstance(inner, lal.BasicSubpDecl):
                kind = "subprogram"
            elif isinstance(inner, lal.PackageDecl) or \
                    isinstance(inner, lal.BasePackageDecl):
                kind = "package"
            self.add_lines([''])
            self.add_lines([f":formal_kind: {kind}"])
            self.handle_entity(inner)
            return
        else:
            print(f"WARNING: Non handled entity: {decl}")

        with self.indent():
            self.add_lines([''] + doc + [''])


if __name__ == '__main__':
    GenerateDoc.run()
