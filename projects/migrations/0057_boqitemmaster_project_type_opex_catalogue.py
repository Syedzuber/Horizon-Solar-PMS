from django.db import migrations, models


# ---------------------------------------------------------------------------
# PART 11 — the OPEX BOQ catalogue.
#
# The 37 rows already in BOQItemMaster (ITM-001..ITM-037, created by 0047 from the
# literal list get_standard_boq_items() used to return) are RESIDENTIAL items and are
# not touched here beyond being stamped with the type they have always had. The 207
# rows below are the design team's OPEX tender catalogue, imported from PMS_BOQ.xlsx
# sheet "BOM " rows 3-209.
#
# WHY A LITERAL AND NOT THE SPREADSHEET. Same reason 0047 carries one: a migration that
# reads a file at apply time cannot be applied on Railway, where the file does not
# exist, and could not be re-applied identically a year from now. The parse was done
# once, verified against the supplied interaction prototype's catalogue (an exact
# 207/207 match on code, category, description and unit), and frozen here.
#
# WHAT THE IMPORT NORMALISED, AND WHAT IT DID NOT:
#
#   UNITS ARE NORMALISED. The source spells seven units nine ways; `Nos.` -> `Nos` and
#   `Mtr.` -> `Meter` are applied, leaving exactly Nos / Meter / Pkt / Set / KWp / Pair /
#   Kg. Two spellings of one unit would otherwise produce two lines in Part 6
#   aggregation, which sums by catalogue row and reports the unit alongside.
#
#   WHITESPACE IS COLLAPSED. The source has embedded newlines and runs of spaces inside
#   descriptions (every DCDB row, for one). Runs of whitespace become a single space and
#   the result is stripped. That is the ONLY change made to any description.
#
#   DESCRIPTIONS ARE OTHERWISE VERBATIM, INCLUDING THE DUPLICATES. `4Sqmm*Cu`,
#   `6Sqmm*Cu`, `10Sqmm*Cu` and `16Sqmm*Cu` each appear twice — once under `Pin Type Lug`
#   and once under `Ring Type Lug` — and they stay as written, as eight rows with eight
#   distinct codes. They are not prefixed, suffixed or deduplicated: a pin lug and a ring
#   lug of the same conductor size are different parts that a storeman must not be handed
#   under one line, and every screen shows category beside description, which tells them
#   apart. Source typos are preserved for the same reason ("proctected", "Bllast",
#   "Hight") — the catalogue must match what the design team wrote.
#
# CODES ARE OPX-001..OPX-207 IN SPREADSHEET ROW ORDER, and sort_order matches the code.
# Category order therefore follows the spreadsheet without being stored anywhere: the
# screens derive it from sort_order (see models.opex_catalogue_category_order).
# ---------------------------------------------------------------------------

#: (category, description, unit) in spreadsheet row order. Index + 1 is the code number
#: and the sort_order. Do not reorder — the codes are stable identifiers that BOQ rows,
#: and from Part 6 grouped procurement, refer to.
OPEX_BOQ_ITEMS = [
    ('Module', 'Solar PV Module', 'Nos'),
    ('DCDB', '5 In/ 5 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('DCDB', '7 In/ 7 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('DCDB', '10 In/ 10 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('DCDB', '12 In/ 12 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('DCDB', '15 In/ 15 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('DCDB', '20 In/ 20 Out DCBDB with DC SPD type-2, 1000 DC Volt and 16Amp Fuse protection in +ve and -ve, with MC4 connector for String Termination', 'Nos'),
    ('Inverter', '12 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '15 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '17 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '20 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '25 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '30 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '33 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '36 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '40 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '50 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '60 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '75 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '100 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '110 kW Grid -Tie Inverter @3P', 'Nos'),
    ('Inverter', '125 kW Grid -Tie Inverter @3P', 'Nos'),
    ('MMS', '2P*3*500mm height', 'Nos'),
    ('MMS', '2P*4*500mm height', 'Nos'),
    ('MMS', '2P*5*500mm height', 'Nos'),
    ('MMS', '2P*6*500mm height', 'Nos'),
    ('MMS', '2P*3*1000mm height', 'Nos'),
    ('MMS', '2P*4*1000mm height', 'Nos'),
    ('MMS', '2P*5*1000mm height', 'Nos'),
    ('MMS', '2P*6*1000mm height', 'Nos'),
    ('MMS', '2P*3*1500mm height', 'Nos'),
    ('MMS', '2P*4*1500mm height', 'Nos'),
    ('MMS', '2P*5*1500mm height', 'Nos'),
    ('MMS', '2P*6*1500mm height', 'Nos'),
    ('MMS', '3P Structure', 'Nos'),
    ('MMS', 'Other Structure', 'Nos'),
    ('MMS', 'Ballast Type Structure', 'KWp'),
    ('MMS', 'Tin Shade', 'KWp'),
    ('ACDB', 'ACDB Type-1 (For 12KW INV), 1 Nos. with MCB: 4P 415V, 25A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 15KW INV), 1 Nos. with MCB: 4P 415V, 30A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 17/20KW INV), 1 Nos. with MCB: 4P 415V, 40A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 25/30KW INV), 1 Nos. with MCCB: 4P 415V, 63A.With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 33/36/40KW INV), 1 Nos. with MCCB: 4P 415V, 100A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 50KW INV), 1 Nos. with MCCB: 4P 415V, 110A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 60/75KW INV), 1 Nos. with MCCB: 4P 415V, 150A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 100KW INV), 1 Nos. with MCB: 4P 415V, 200A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 110KW INV), 1 Nos. with MCB: 4P 415V, 225A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-1 (For 125KW INV), 1 Nos. with MCB: 4P 415V, 250A. With LSIG with Earth fault protection,SPD', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 12KW INV), 1 Nos. with MCB: 4P 415V, 25A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 15KW INV), 1 Nos. with MCB: 4P 415V, 30A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 17/20KW INV), 1 Nos. with MCB: 4P 415V, 40A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 25/30KW INV), 1 Nos. with MCCB: 4P 415V, 63A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 33/36/40KW INV), 1 Nos. with MCCB: 4P 415V, 100A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 50KW INV), 1 Nos. with MCCB: 4P 415V, 110A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 60/75KW INV), 1 Nos. with MCCB: 4P 415V, 150A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 100KW INV), 1 Nos. with MCB: 4P 415V, 200A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 110KW INV), 1 Nos. with MCB: 4P 415V, 225A.', 'Nos'),
    ('ACDB', 'ACDB Type-2 (For 125KW INV), 1 Nos. with MCB: 4P 415V, 250A.', 'Nos'),
    ('DC Cable', '1C x 4 Sq.mm 1.8KV DC Red , XLPO, CU Cable, TUV Approved, Solar grade cables-IEC 60189-1, IEC 60189-2, EBXL, UV protected, FRLS & anti rodent', 'Meter'),
    ('DC Cable', '1C x 4 Sq.mm 1.8KV DC Black , XLPO, CU Cable, TUV Approved, Solar grade cables-IEC 60189-1, IEC 60189-2 EBXL, UV protected, FRLS & anti rodent', 'Meter'),
    ('DC Cable', 'Multicontact MC4 Connectors suitable for terminating 4/6 Sq. mm Copper cables (Male + Female Connector)', 'Pair'),
    ('AC Cable', '1.1kV*4C*4Sqmm*Cu*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*6Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*10Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*16Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*25Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*35Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*50Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*70Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*95Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*120Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*150Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*185Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*240Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*4C*300Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*25Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*35Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*50Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*70Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*95Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*120Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*150Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*185Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*240Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*300Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*1C*300Sqmm*Cu*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*1C*4Sqmm*Cu*XLPE*Wire For Earthing cable', 'Meter'),
    ('AC Cable', '1.1kV*1C*16Sqmm*Cu*XLPE*Wire For Earthing cable', 'Meter'),
    ('AC Cable', '1.1kV*1C*70Sqmm*Cu*XLPE*Wire For Earthing cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*25Sqmm*AL*XLPE*cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*35Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*50Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*70Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*95Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*120Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*150Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*185Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*240Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*3.5C*300Sqmm*AL*XLPE*Cable', 'Meter'),
    ('AC Cable', '1.1kV*1C*300Sqmm*AL*XLPE*Cable', 'Meter'),
    ('Pin Type Lug', '4Sqmm*Cu', 'Nos'),
    ('Pin Type Lug', '6Sqmm*Cu', 'Nos'),
    ('Pin Type Lug', '10Sqmm*Cu', 'Nos'),
    ('Pin Type Lug', '16Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '4Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '6Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '10Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '16Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '25Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '35Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '50Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '70Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '95Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '120Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '150Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '185Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '240Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '300Sqmm*Cu', 'Nos'),
    ('Ring Type Lug', '16Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '25Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '35Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '50Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '70Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '95Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '120Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '150Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '185Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '240Sqmm*AL', 'Nos'),
    ('Ring Type Lug', '300Sqmm*AL', 'Nos'),
    ('Conduit', '25 MM PVC Conduit Pipe UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'PVC Elbow 25MM', 'Nos'),
    ('Conduit', 'PVC Tee 25MM', 'Nos'),
    ('Conduit', '32 MM PVC Conduit Pipe UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'PVC Elbow 32 MM', 'Nos'),
    ('Conduit', 'PVC Tee 32 MM', 'Nos'),
    ('Conduit', '40 MM PVC Conduit Pipe UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'PVC Elbow 40MM', 'Nos'),
    ('Conduit', 'PVC Tee 40MM', 'Nos'),
    ('Conduit', '50 MM PVC Conduit Pipe UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'PVC Elbow 50MM', 'Nos'),
    ('Conduit', 'PVC Tee 50MM', 'Nos'),
    ('Conduit', 'Flexible conduit 25mm PVC UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'Flexible conduit 32mm PVC UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'Flexible conduit 40mm PVC UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'Flexible conduit 50mm PVC UV proctected and FRLS Grade', 'Meter'),
    ('Conduit', 'SS Saddle for 25MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'SS Saddle for 32MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'SS Saddle for 40MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'SS Saddle for 50MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'PVC Clamp for 25MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'PVC Clamp for 32MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'PVC Clamp for 40MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'PVC Clamp for 50MM conduit (100PCS)', 'Pkt'),
    ('Conduit', 'GI screw for saddle fixing (100PCS)', 'Pkt'),
    ('Conduit', 'PVC gitti for saddle fixing (100PCS)', 'Pkt'),
    ('Conduit', '50MM Flexible DWC pipe for underground cable', 'Meter'),
    ('Conduit', '100MM Flexible DWC pipe for underground cable', 'Meter'),
    ('Conduit', '150MM Flexible DWC pipe for underground cable', 'Meter'),
    ('Conduit', '200MM Flexible DWC pipe for underground cable', 'Meter'),
    ('Conduit', 'HDPE PIPE 50 MM WITH 2 END CAP With Clamps for Fixing', 'Meter'),
    ('Conduit', 'HDPE PIPE 75 MM WITH 2 END CAP With Clamps for Fixing', 'Meter'),
    ('Conduit', 'HDPE PIPE 100 MM WITH 2 END CAP With Clamps for Fixing', 'Meter'),
    ('Cable Tray', 'HDGI/ZAM, Perforated type, Cable Tray 50mm wide, 50mm Hight,1.6mm Thick with cover and connecting plate & nutbolt & other installation item.', 'Meter'),
    ('Cable Tray', 'HDGI/ZAM, Perforated type, Cable Tray 100mm wide, 50mm Hight,1.6mm Thick with cover and connecting plate & nutbolt & other installation item.', 'Meter'),
    ('Cable Tray', 'HDGI/ZAM, Perforated type, Cable Tray 150mm wide, 50mm Hight,1.6mm Thick with cover and connecting plate & nutbolt & other installation item.', 'Meter'),
    ('Cable Tray', 'HDGI/ZAM, Perforated type, Cable Tray 200mm wide, 50mm Hight,1.6mm Thick with cover and connecting plate & nutbolt & other installation item.', 'Meter'),
    ('Cable Tray', 'PVC Cable Tray 50mm wide with cover', 'Meter'),
    ('Earthing', '25x3 GI Strip (min. 80 micron)', 'Meter'),
    ('Earthing', 'M8 x 25mm GI Nut Bolt with 2 nos Washers of 10mm Dia (To joint earthing Strip eand )', 'Set'),
    ('Earthing', 'Copper Bonded Earthing Electrode 17.2 MM dia having 250 micron Cu coating, 3 mtrs long', 'Nos'),
    ('Earthing', 'Copper Bonded Earthing Electrode 25 MM dia having 250 micron Cu coating, 2 mtrs long', 'Nos'),
    ('Earthing', '300MM X 300MM Square Type Precast concrete enclosure with Earthing Chamber Cover', 'Set'),
    ('Earthing', '25Kg Chemical Earthing Compound Bags BFC', 'Nos'),
    ('Earthing', '40mm Height Epoxy Insulator for 25 X 3 MM Earth strip installation With Nut Bolt', 'Nos'),
    ('Earthing', 'Conventional type Lightning Arresters , as per NFC 17-102:2011, with all necessory accessories', 'Nos'),
    ('Earthing', 'ESE Type II Lightning Arresters , as per NFC 17-102:2011, with all necessory accessories LA diameter 107 Meter and mast height of 5Meter.', 'Nos'),
    ('Earthing', '1 Set of Nut and Bolt 4mm diax20mm length, 1 Teeth Washer,1 Cut Washer and 1 Plain Washer washer outerdia 10mm & inner dia 4mm around (For Module to Module earthing)', 'Set'),
    ('Solar Meter + CT', 'Below 20KW Inverter CT is not required. Solar Generation Meter with Accuracy class 1.0s, and RS 485/RS 232 compatible with enclosure box, suitable for communication with data logger (Saral 305 Sequre meter)', 'Nos'),
    ('Solar Meter + CT', 'Above 20KW Inverter CT is required. Solar Generation Meter with Accuracy class 0.5s, and RS 485/RS 232 compatible with enclosure box, suitable for communication with data logger . (Secure Premier 300 LT CT)', 'Nos'),
    ('Solar Meter + CT', 'CT 50/5A, 0.5S, 5VA (Set =3 Nos, RYB)', 'Set'),
    ('Solar Meter + CT', 'CT 100/5A, 0.5S, 5VA (Set =3 Nos, RYB)', 'Set'),
    ('Solar Meter + CT', 'CT 150/5A, 0.5S, 5VA (Set =3 Nos, RYB)', 'Set'),
    ('Solar Meter + CT', 'CT 200/5A, 0.5S, 5VA (Set =3 Nos, RYB)', 'Set'),
    ('Data Logger+ WMS', "Data Logger RS485 2No's + Ethernet + 4 AI (4-20 mA ) + 4 DI/O + USB", 'Nos'),
    ('Data Logger+ WMS', 'Supply of 4Pair twisted, 0.5 Sq.mm Shielded/Armoured RS485 Cable', 'Meter'),
    ('Data Logger+ WMS', 'WMS (Data Logger) for Sensor communication (a) Solar Irradiance sensor (b) Ambient temperature sensor (c ) Wind Speed sensor', 'Set'),
    ('Civil', 'Underground cable laying in 500 mm depth (including digging, refilling, cable laying in DWC conduit, etc.)', 'Meter'),
    ('BOS', 'Cable Tie 250 MM (PVC & UV resistant)', 'Pkt'),
    ('BOS', 'Cable Tie 400 MM (PVC & UV resistant)', 'Pkt'),
    ('BOS', 'Cable Tie 500 MM (SS)', 'Pkt'),
    ('BOS', 'Cast iron, Cable Route marker (Printed as "Solar cable")', 'Nos'),
    ('BOS', 'PVC Tape (Red, Black, Yellow & Green)', 'Nos'),
    ('BOS', 'Silver Spray Paint', 'Kg'),
    ('BOS', 'Fasteners for Inverter/DCDB/ACDB Mounting', 'Nos'),
    ('BOS', 'Ferules (A-Z) (0-9)', 'Nos'),
    ('BOS', 'GENERIC 750 ml Can PU Foam Sealant Spray for Joint filling', 'Nos'),
    ('BOS', 'Fire Extinguisher 4KG - ABC Type', 'Nos'),
    ('BOS', 'DANGER BOARDS', 'Nos'),
    ('BOS', 'Display BOARDS', 'Nos'),
    ('BOS', 'CCTV for project monitoring (either on rent or purchased)', 'Nos'),
    ('BOS', 'Walk way 350mm with mounting attachment', 'Nos'),
    ('BOS', 'Safety rail with mounting attachment', 'Nos'),
    ('BOS', 'INC work 2P structure', 'KWp'),
    ('BOS', 'INC work Tin shade', 'KWp'),
    ('BOS', 'INC work Bllast struture', 'KWp'),
    ('BOS', 'Insulating Rubber Mat 500*1000', 'Nos'),
    ('BOS', 'Insulating Rubber Mat 1000*1000', 'Nos'),
]

#: Guards the import against a silently truncated or reordered literal. Checked before
#: anything is written, so a mismatch fails the migration rather than half-populating the
#: catalogue. These are the counts stated in the Part 11 brief.
EXPECTED_CATEGORY_COUNTS = {
    'Module': 1, 'DCDB': 6, 'Inverter': 15, 'MMS': 16, 'ACDB': 20, 'DC Cable': 3,
    'AC Cable': 39, 'Pin Type Lug': 4, 'Ring Type Lug': 25, 'Conduit': 33,
    'Cable Tray': 5, 'Earthing': 10, 'Solar Meter + CT': 6, 'Data Logger+ WMS': 3,
    'Civil': 1, 'BOS': 20,
}

#: The seven units the import is allowed to produce, after normalisation.
EXPECTED_UNITS = {'Nos', 'Meter', 'Pkt', 'Set', 'KWp', 'Pair', 'Kg'}

#: The code prefix that identifies a row this migration created. The reverse deletes by
#: this AND project_type, never by project_type alone — an OPEX row added later through
#: the admin catalogue screen is somebody's data, not ours to remove.
OPEX_CODE_PREFIX = 'OPX-'


def _check_literal():
    """Fail loudly before writing anything if the literal is not what it should be."""
    if len(OPEX_BOQ_ITEMS) != 207:
        raise ValueError(f'OPEX_BOQ_ITEMS has {len(OPEX_BOQ_ITEMS)} rows, expected 207.')

    counts = {}
    for category, _description, _unit in OPEX_BOQ_ITEMS:
        counts[category] = counts.get(category, 0) + 1
    if counts != EXPECTED_CATEGORY_COUNTS:
        raise ValueError(f'OPEX category counts {counts} != expected '
                         f'{EXPECTED_CATEGORY_COUNTS}.')

    units = {unit for _category, _description, unit in OPEX_BOQ_ITEMS}
    if not units <= EXPECTED_UNITS:
        raise ValueError(f'Unnormalised unit(s) in the literal: '
                         f'{sorted(units - EXPECTED_UNITS)}.')
    return counts


def scope_and_import(apps, schema_editor):
    """Stamp the existing catalogue Residential, then create the 207 OPEX rows."""
    BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')
    counts = _check_literal()

    # EXPLICITLY, not by relying on the field default. AddField has already backfilled
    # every existing row with 'Residential', but a default is a schema fact and this is a
    # data fact — if the default is ever changed, the 37 rows must still be Residential.
    scoped = BOQItemMaster.objects.exclude(project_type='Residential').update(
        project_type='Residential')
    print(f'\n[0057] Scoped {scoped} pre-existing catalogue row(s) to Residential '
          f'(total Residential now {BOQItemMaster.objects.filter(project_type="Residential").count()}).')

    # Idempotent: a re-run (or a partially applied migration) must not duplicate codes.
    existing = set(BOQItemMaster.objects
                   .filter(code__startswith=OPEX_CODE_PREFIX)
                   .values_list('code', flat=True))
    if existing:
        print(f'[0057] {len(existing)} OPX- row(s) already present — skipping those.')

    to_create = []
    for index, (category, description, unit) in enumerate(OPEX_BOQ_ITEMS, start=1):
        code = f'{OPEX_CODE_PREFIX}{index:03d}'
        if code in existing:
            continue
        to_create.append(BOQItemMaster(
            code=code,
            description=description,
            unit=unit,
            category=category,
            project_type='OPEX',
            is_active=True,
            sort_order=index,
        ))
    BOQItemMaster.objects.bulk_create(to_create)

    print(f'[0057] Created {len(to_create)} OPEX catalogue row(s), by category:')
    for category in EXPECTED_CATEGORY_COUNTS:
        print(f'         {category:<18} {counts[category]:>4}')
    print('[0057] Final catalogue count by project type:')
    for project_type in ('Residential', 'OPEX', 'CAPEX'):
        total = BOQItemMaster.objects.filter(project_type=project_type).count()
        if total:
            print(f'         {project_type:<18} {total:>4}')


def drop_import(apps, schema_editor):
    """Reverse: remove ONLY the 207 rows this migration created.

    Deletes by code prefix AND project_type, so an OPEX catalogue row somebody added
    afterwards through the admin screen survives. Touches no Residential row and no
    BOQItem: BOQItem.item_master is SET_NULL, so any BOQ line built from a deleted OPEX
    row keeps its description, quantity and serial number and simply loses the catalogue
    link — the same behaviour 0047's reverse has.
    """
    BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')
    BOQItem       = apps.get_model('projects', 'BOQItem')

    doomed = BOQItemMaster.objects.filter(
        project_type='OPEX', code__startswith=OPEX_CODE_PREFIX)
    orphaned = BOQItem.objects.filter(item_master__in=doomed).count()
    removed = doomed.count()
    doomed.delete()

    # WITHOUT THIS THE REVERSE CANNOT COMPLETE ON POSTGRES. The whole migration runs in
    # one transaction, and the delete above leaves BOQItem's deferred item_master FK
    # trigger events pending. The very next operation to be undone is the AddField, whose
    # backward is an ALTER TABLE on this same table — and Postgres refuses that with
    # "cannot ALTER TABLE ... because it has pending trigger events". Forcing the deferred
    # constraints to be checked here drains them while the transaction is still open, so
    # the ALTER succeeds and the reverse stays atomic. Nothing is committed early.
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    residential = BOQItemMaster.objects.filter(project_type='Residential').count()
    print(f'\n[0057 reverse] Deleted {removed} OPEX catalogue row(s). '
          f'{residential} Residential row(s) untouched. '
          f'{orphaned} BOQItem row(s) kept, item_master set null by SET_NULL.')


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0056_part9_1_scoped_rework'),
    ]

    operations = [
        migrations.AddField(
            model_name='boqitemmaster',
            name='project_type',
            field=models.CharField(
                choices=[('Residential', 'Residential'), ('OPEX', 'OPEX'),
                         ('CAPEX', 'CAPEX')],
                db_index=True, default='Residential', max_length=20),
        ),
        migrations.RunPython(scope_and_import, drop_import),
    ]
