package Pkg is
   type Flags is record
      A : Boolean;
      B : Boolean;
   end record;
   for Flags'Size use 16;
   --  AttributeDefClause: ``for X'Size use N``.
end Pkg;
