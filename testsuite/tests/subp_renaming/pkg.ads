package Pkg is
   function Real (X : Integer) return Integer is (X * 2);
   function Aliased_Real renames Real;
   procedure Real_Proc (X : Integer);
   procedure Aliased_Real_Proc renames Real_Proc;
   --  Two subprogram renamings: one function, one procedure.
end Pkg;
