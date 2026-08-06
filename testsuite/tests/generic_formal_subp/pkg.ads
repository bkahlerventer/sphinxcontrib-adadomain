generic
   type F is private;
   with function Op (X : F) return Integer is <>;
package Pkg is
end Pkg;
--  Generic package with a type formal AND a subprogram formal
--  with a default (``is <>``). Verifies that :formal_kind:
--  body fields correctly tag both as their respective kinds.
