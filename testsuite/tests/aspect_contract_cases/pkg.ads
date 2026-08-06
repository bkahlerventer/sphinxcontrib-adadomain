package Pkg is
   function Classify (X : Integer) return Integer
     with Contract_Cases =>
        (X > 0    => Classify'Result =  1,
         X = 0    => Classify'Result =  0,
         others   => Classify'Result = -1);
   --  Contract_Cases: whole aggregate AND per-case breakdown.
end Pkg;
