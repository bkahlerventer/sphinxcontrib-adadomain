package Pkg is
   type Message_T (Kind : Integer := 0) is record
      case Kind is
         when 0 =>
            Length : Natural;
         when others =>
            Raw : Natural;
      end case;
   end record;
   --  Single-variant case: one when clause and one others.
end Pkg;
