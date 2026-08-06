package Pkg is
   function Check (X : Integer) return Integer
     with Pre  => X > 0,
          Post => Check'Result >= 0;
   --  Pre and Post aspects on a function.
end Pkg;
