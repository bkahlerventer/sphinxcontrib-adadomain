package Pkg is
   protected type Counter is
      function Get return Natural;
      entry Wait;
   private
      N : Natural := 0;
   end Counter;
end Pkg;
