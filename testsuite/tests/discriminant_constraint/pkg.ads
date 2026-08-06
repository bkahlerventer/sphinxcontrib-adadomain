package Pkg is
   type R (D : Integer := 0) is record
      A : Integer;
   end record;
   type A is access all R (D => 0);
   --  Access type with a discriminant constraint.
end Pkg;
