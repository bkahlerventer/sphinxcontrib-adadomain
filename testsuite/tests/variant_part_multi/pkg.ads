package Pkg is
   type Message_T (Kind : Integer := 0) is record
      case Kind is
         when 0 =>
            Length : Natural;
            Data   : Integer;
         when 1 | 2 =>
            Width  : Natural;
            Height : Natural;
         when others =>
            Raw : Natural;
      end case;
   end record;
   --  Multi-variant: three when clauses, including 1 | 2.
end Pkg;
