package Pkg is
   type R is record
      A : Integer;
      B : Integer;
   end record;
   for R use record
      A at 0 range 0 .. 31;
      B at 4 range 0 .. 31;
   end record;
   --  Two ComponentClauses: A at byte 0, B at byte 4.
end Pkg;
