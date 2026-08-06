package Pkg is
   function Foo (X : Integer) return Integer;
   function Foo (X : Integer; Y : Integer) return Integer;
   function Foo (S : String) return Integer;
   --  Three overloaded Foo functions with distinct parameter
   --  profiles. Tier 3 collision-keying keeps all three
   --  reachable via :ada:func:`Foo`.
end Pkg;
