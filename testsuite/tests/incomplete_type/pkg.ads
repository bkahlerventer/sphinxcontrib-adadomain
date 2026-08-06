package Pkg is
   type Forward;
   type Forward_Ptr is access all Forward;
   type Forward is record
      Next : Forward_Ptr;
   end record;
   --  Incomplete type: ``type Forward;`` declares the forward
   --  reference; the full definition appears later in the same
   --  package.
end Pkg;
