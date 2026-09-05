"""
Management command: seed the seven real HRPPL installation checklists onto the OPEX
Installation phase.

    python manage.py seed_opex_installation_checklists --dry-run   # plan only, writes nothing
    python manage.py seed_opex_installation_checklists             # create

ONE-TIME CONTENT SEED, AND DELIBERATELY NOT A MIGRATION
-------------------------------------------------------
This is content, not schema. 0041 built the Checklist/ChecklistItem/ChecklistTaskLink
mechanism, 0077 re-keyed the link onto template_task, and 0081 added sections, answers
and the per-checklist photo flag. All of it has been sitting empty for OPEX because
nobody had decided WHICH lines go on WHICH task. These documents are that decision,
arriving from the Tenders/QA-QC side as eleven PDFs, and they will be revised -- R00 is
stamped on every one of them. Content that gets revised belongs behind Checklist's
versioning (author v2, activate it, v1 archives), NOT behind a migration that can only
be replayed by writing another migration.

It is safe to run anywhere, unlike `seed_opex_test_data`: it creates no projects, no
users and no demo namespace, only the reference content the product was built to hold.
It is idempotent by checklist `code` -- a second run skips what already exists and says
so, rather than creating a second family or a duplicate link.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
RMS Installation and Solar Generation Meter Installation are the two Installation-phase
tasks with no checklist. RMS was deferred to a later version (the Scada-RMS document
exists and is not seeded); metering has no source document at all. Neither gets an empty
placeholder Checklist, because an empty checklist and no checklist are different things
to `_checklist_for_task()` -- it returns None for an unlinked task, the task renders with
no checklist section, and completion is unaffected. A placeholder would instead render a
checklist with nothing in it, which reads as "somebody forgot".

FOUR TASKS TAKE TWO SOURCE DOCUMENTS EACH
-----------------------------------------
ChecklistTaskLink allows exactly one checklist per task, twice over: unique_together
(task_name, project_type) and the partial unique index on template_task. So Civil Work,
LA and Earthing, DC Cable Laying and DCDB/ACDB each merge two documents into ONE
checklist, and `ChecklistItem.section` is what keeps the halves apart on screen. AC-DC
Cables goes the other way and splits: its DC half joins the SCB/AJB document on DC Cable
Laying, its AC half is the whole of AC Cable Laying.

ITEM TEXT IS TRANSCRIBED, NOT SUMMARISED
----------------------------------------
Every label below is the source document's check text, rejoined where the PDF wrapped it
across lines. Nothing is reworded, shortened or corrected -- including the places where
the source is itself truncated (SCB item 15 ends "as per IS /") or misspelt ("veified").
This is a QA record: an engineer answering line 23 must be answering the line the paper
form asks, and a helpful edit here is an undetectable drift between the two. Document
headers, Doc. No. blocks, page footers and the Checked-By/Witnessed-By sign-off grid are
NOT items and are not transcribed.

Where a source groups items under a sub-heading that has nowhere to live -- Structure's
"Purlin"/"Truss assembly"/"Sag Rod" sit two levels deep and `section` is one level -- the
sub-heading is carried as a label prefix. Without it "Purlin twist" appears twice in one
checklist with nothing to say which is the Fixed and which the Tracker copy.

NO PHOTO IS REQUIRED ON ANY OF THE SEVEN
----------------------------------------
`requires_photo=False` throughout, which is 0081's per-checklist flag and not a global
one. It is a reading of the documents rather than a preference: all eleven carry Yes/No
+ Remarks columns and a separate three-signature sign-off block, and not one has a
per-item evidence column. Demanding a photograph per line would be this system inventing
a requirement the paper form does not make.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from projects.models import (
    Checklist, ChecklistItem, ChecklistTaskLink, TaskTemplate, TaskTemplateTask,
)


# ---------------------------------------------------------------------------
# Source: I&C Check list for Concrete block_R00_23-8-26.pdf  (HRPPL/CIVIL/BLOCK)
# ---------------------------------------------------------------------------
SEC_BLOCK = 'Concrete Block (RCC/PCC)'
CONCRETE_BLOCK = [
    'Location Marking as per approved MMS layout GA/drawing',
    'Block Dimensions as per approved GA drawings of MMS',
    'Farma/template Properly aligned and leak-proof and lubricants to be used inside surface surroundings',
    'Reinforcement Placement as per structural drawing (if applicable)',
    'Concrete Grade M20/M25 or as per approved grade or design mix.',
    'Concrete mixing with mixture machine or doing manual',
    'Surface finish is smooth and in proper level',
    'Honeycombing (air voids) should not observed, vibrator use required',
    'No visible cracks to be observed',
    'Curing min 7 days to 14 days',
    'Column/Leg to be in Centre to centre as per drawing',
    'MMS installation after proper curing min 7 days or as per strength of constructed blocks at site',
    'Cube test to be performed as per approved design mix for 7 days to 28 days',
]

# ---------------------------------------------------------------------------
# Source: I&C Check list for Structure_R00_26-6-26.pdf  (HRPPL/ELE/STR)
# Its three top-level headings (I, II, III) become the three sections.
# ---------------------------------------------------------------------------
SEC_STR_TYPE    = 'Type of PV Module Structure Installed'
SEC_STR_FIXED   = 'Fixed type structure Installation'
SEC_STR_TRACKER = 'Tracker type structure Installation'

STRUCTURE = [
    (SEC_STR_TYPE, 'Fixed type assembly(2P/Ballast type)'),
    (SEC_STR_TYPE, 'Tracker type assembly(if applicable)'),

    (SEC_STR_FIXED, 'Structure Installation - Check structure parts free from rust.'),
    (SEC_STR_FIXED, 'Structure Installation - Check post, purlin dimensions as per drawings'),
    (SEC_STR_FIXED, 'Structure Installation - Check post position verification'),
    (SEC_STR_FIXED, 'Structure Installation - Check Grading max-slope as per design/drawings'),
    (SEC_STR_FIXED, 'Structure Installation - Check Row layout & modules per row'),
    (SEC_STR_FIXED, 'Structure Installation - Check Nut, bolts torque applied as per specifications/design'),
    (SEC_STR_FIXED, 'Structure Installation - Check Gaps between arrays as per drawing'),
    (SEC_STR_FIXED, 'Structure Installation - Check all accessories are fixed as per drawing & as per part identification table'),
    (SEC_STR_FIXED, 'Structure Installation - Earthing done for the structures & mounting equipment'),
    (SEC_STR_FIXED, 'Structure Installation - Check for Earth strip sizes used and connections done as per approved drawings'),
    (SEC_STR_FIXED, 'Purlin - Purlin over all alignment'),
    (SEC_STR_FIXED, 'Purlin - Purlin straightness'),
    (SEC_STR_FIXED, 'Purlin - Purlin twist'),

    (SEC_STR_TRACKER, 'Vertical Structure Installation - Pitch distance of vertical purlins as per design/drawing'),
    (SEC_STR_TRACKER, 'Vertical Structure Installation - Straightness of structure'),
    (SEC_STR_TRACKER, 'Vertical Structure Installation - Check Tilt angle as per design/drawing'),
    (SEC_STR_TRACKER, 'Vertical Structure Installation - Check for the tightness of fixing nut-bolts : Torque value applied as per design/specification and torque marking'),
    (SEC_STR_TRACKER, 'Vertical Structure Installation - Bar tighten by binding wire'),
    (SEC_STR_TRACKER, 'Truss assembly - Truss assembly installed as per drawings'),
    (SEC_STR_TRACKER, 'Truss assembly - Check joints of truss assembly connected properly (if applicable)'),
    (SEC_STR_TRACKER, 'Truss assembly - Check for truss alignment'),
    (SEC_STR_TRACKER, 'Truss assembly - Check for the tightness of fixing nut-bolts : Torque value applied as per design/specification and torque marking'),
    (SEC_STR_TRACKER, 'Truss assembly - Truss bottom chord tie member properly connected'),
    (SEC_STR_TRACKER, 'Purlin - Purlin overall alignment'),
    (SEC_STR_TRACKER, 'Purlin - Purlin straightness'),
    (SEC_STR_TRACKER, 'Purlin - Purlin twist'),
    (SEC_STR_TRACKER, 'Purlin - Purlin member overhanging'),
    (SEC_STR_TRACKER, 'Sag Rod - Check sag rod is used'),
    (SEC_STR_TRACKER, 'Sag Rod - Location of sag rod as per drawing'),
    (SEC_STR_TRACKER, 'Sag Rod - Sag rod bolted & tightened properly'),
    (SEC_STR_TRACKER, 'Sag Rod - Check for the tightness of fixing nut-bolts : Torque value applied as per design/specification and torque marking'),
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist  for PV Modules_R00_26-6-26.pdf  (HRPPL/ELE/I&C/MOD)
# No headings in the source, so no sections. Items 14 and 15 are parent lines
# with lettered children; the parent is kept and each child carries its parent's
# phrase so the line still reads on its own.
# ---------------------------------------------------------------------------
PV_MODULES = [
    'Check PV Modules installed as per row layout (Refer approved drawings & Specifications)',
    'Check for Module "Wp" rating as per Approved drawing/ Datasheet/Specifications',
    'Check for availability of OEM Module installation Guide/manual prior to installation',
    'Check for Module any damages or scratches before installation on structure',
    'Check PV Module surface free from scratches and undamaged',
    'Check junction box, cable & connectors undamaged prior to installation',
    'Check before module installation the connectors, inverters and other electrical components/panels are in a disconnect status',
    'Check Module mounting supports alignment & row spacing within tolerance limits (As per specifications)',
    'Check Module clamps fixed properly and washers, Nut- Bolts provided as per approved drawings',
    'Check Modules fixed on supports using lock/spring washers',
    'Check Modules tightened with torque wrench spanner & marking done on nut/bolts (Mention the applied torque value)',
    'Check all module frames & structures earthing as per approved drawing',
    'Check for proper PV connector assembly with PV cable adequately crimped',
    'PV Module wiring/cabling done should be free from following -',
    'PV Module wiring/cabling free from - Cable/ wire Short bends',
    'PV Module wiring/cabling free from - Cable/ wire sagging',
    'PV Module wiring/cabling free from - Hanging of cable/ wire',
    'PV Module wiring/cabling free from - Stress on module junction box',
    'Check for Module Series Wiring & Voc measurement as',
    'Module Series Wiring & Voc measurement - For Right Polarity / No reverse polarity allowed',
    'Module Series Wiring & Voc measurement - Number of modules in series with reference to SLD',
    'Module Series Wiring & Voc measurement - String Voc to match SLD and Datasheet',
    'Module Series Wiring & Voc measurement - Check for any loose contacts & short circuit of module',
    'Module Series Wiring & Voc measurement - Check each module for any hot spot',
    'Check for IR (Insulation resistance) between conductor and earth - From PV array to AJB/ SCB/Inverter',
    'Check whether mixed current binning modules installed',
    'Check the ferrules should be in correct & visible.',
    'Voc & Isc to be checked & recorded after string cable interconnection before putting system on load',
    'Vmp & Imp to be checked and recorded on load',
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist for earthing system_R00_1-7-26.pdf  (HRPPL/ELE/I&C/EAR)
# Three top-level headings; the third is the source's own LA section, which sits
# beside the separate ESE LA document on the same task.
# ---------------------------------------------------------------------------
SEC_EARTH_PITS = 'Earth Pits'
SEC_EARTH_INST = 'Earthing Installation'
SEC_EARTH_LA   = 'Lightning Arrester Installation'

EARTHING = [
    (SEC_EARTH_PITS, 'Location & No. of pits'),
    (SEC_EARTH_PITS, 'Depth& Distance of earth pits'),
    (SEC_EARTH_PITS, 'Electrode Ci/Gi/ Copper Size i) Plate type - 600/1200mm/as per Spec. ii)Pipe type - 40/50/ 100mm/as per Spec. iii. Cu Bonded 17.2 mm dia solid rod'),
    (SEC_EARTH_PITS, 'Charcoal/ Salt'),
    (SEC_EARTH_PITS, 'Welded/Nut-bolts/brazed connections at joints'),
    (SEC_EARTH_PITS, 'Reducer/ Funnel provided'),
    (SEC_EARTH_PITS, 'Test link provided'),
    (SEC_EARTH_PITS, 'Masonry work complete'),
    (SEC_EARTH_PITS, 'Cover installed'),
    (SEC_EARTH_PITS, 'Pit identification number fixed / painted, Mentioning Earthing "used for"'),

    (SEC_EARTH_INST, 'Earth conductor sizes as per drawing/design'),
    (SEC_EARTH_INST, 'Overlap at welded joints/Brazing'),
    (SEC_EARTH_INST, 'Anti-Corrosive paint applied at welded joints'),
    (SEC_EARTH_INST, 'Tightness for Bolted Joints with torque wrench'),
    (SEC_EARTH_INST, 'Bolted Joints marked after tightening'),
    (SEC_EARTH_INST, 'Laying / straightening/ Bending of strips'),
    (SEC_EARTH_INST, 'Removing of paint at equipment termination ends'),
    (SEC_EARTH_INST, 'Earth pits interlinked with strip'),
    (SEC_EARTH_INST, 'Contact surfaces free from non-conducting materials'),
    (SEC_EARTH_INST, 'Check the strip connection with MMS to MMS and MMS to Earth pit'),
    (SEC_EARTH_INST, 'Earth conductors fixing to buildings shall be by means of copper or brass clamps with brass screws and non-fibrous wall plugs'),
    (SEC_EARTH_INST, "Low voltage switch boards & MCCB's shall be connected to the earthing system by two separate &distinct conductors"),
    (SEC_EARTH_INST, 'Enclosure of all transformers & switchgear shall be earthed directly to the main earth by two independent connections'),
    (SEC_EARTH_INST, 'Metallic sheaths and armoring of all multi-cored LV cables shall be directly earthed at both ends'),
    (SEC_EARTH_INST, 'For single core cables - Metallic sheaths and armoring of shall be directly earthed at only one end'),
    (SEC_EARTH_INST, 'Fences shall be separately earthed from other earthing systems on each side of gate'),
    (SEC_EARTH_INST, 'Check trench depth (600 mm) for earthing conductor'),

    (SEC_EARTH_LA, 'Location & No. of pits'),
    (SEC_EARTH_LA, 'Depth& Distance of earth pits'),
    (SEC_EARTH_LA, 'Check for Location of foundation &depth for arrestor pole as per approved drawing'),
    (SEC_EARTH_LA, 'Fixing of LA pole on foundation & proper tightening'),
    (SEC_EARTH_LA, 'Check for connection & tightening of LA down conductor with earth pit Gi strip'),
    (SEC_EARTH_LA, 'Down conductor connected to the earth pits shall be provided with insulator supports'),
    (SEC_EARTH_LA, 'Earthing Test Links shall be provided as per Earthing layout drawings'),
    (SEC_EARTH_LA, 'Tightness for Bolted Joints with torque wrench'),
    (SEC_EARTH_LA, 'Check LA pole provided with stray wires supports'),
    (SEC_EARTH_LA, 'Check that OEM Manuals & Test Reports are available at the time of installation'),
    (SEC_EARTH_LA, 'Check the identification of earth pits'),
    (SEC_EARTH_LA, 'Separate earthing for AC, DC & LA provided'),
    (SEC_EARTH_LA, 'ERT test to be performed and recorded for all earthing pits'),
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist for ESE LA_R00_1-7-26.pdf  (HRPPL/ELE/I&C/ESE LA)
# ---------------------------------------------------------------------------
SEC_ESE_LA = 'ESE Type Lightning Arrestor'
ESE_LA = [
    'Check the LA type, Rating & Make as per approved drawings / specifications',
    'Check for physical damage of LA & its assembly/parts before InC',
    'Check for correctness of foundation as per drawings',
    'Check for correctness of structure installation(Mast & accessories) & alignment as per drawing/specifications',
    'Check for grouting of foundation bolts completed',
    'Check ESE terminal with adaptor with flat copper strip or standard copper wire',
    'Check for stay support with mast.',
    'Check for availability of OEM Manuals & Test Reports while Installations',
    'Check for tightness of equipment base with nut-bolts and correct torque applied as per specifications',
    'Check for availability of Surge Counter Installation Manual and Test Reports',
    'Check for Earth strip/wire sizes as per approved drawings',
    'Check that surge monitor/counter connected with earth pit/Discharger',
    'Check for proper earthing of equipment',
    'Earth Continuity Check (Measured Value)/earthing value',
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist For SCB Or AJB_R00_1-7-26.pdf  (HRPPL/ELE/I&C/SCB-AJB)
# ---------------------------------------------------------------------------
SEC_SCB = 'String Combiner Box / Array Junction Box'
SCB_AJB = [
    'Check combiner box installed of voltage class & type as per approved drawings / specifications and approved',
    'Check connectors used for DC application should be correctly rated of voltages & currents as per drawings / specifications',
    'Check for the tightness of clamps, supports, Nut-bolts used for combiner box',
    'Check Cables/wires from module to combiner box should be of correct type and routed in correct section as per approved drawing / SLD',
    'Check the cables protected & secured from physical damage and all sharp edges',
    'Check that Bimetallic lugs used for DC output power cable terminations inside SCB',
    'Check the cables arranged & supported as per drawings/specifications',
    'Check the cable bending radius strictly as per IS / Specifications (Avoid sharp cable bends)',
    'Check for tightness of weather proof cable glands to combiner box gland plate',
    'Check for No joints allowed in wire/cable between module JB to Combiner box',
    'Check each DC cable electrical connection/termination should be clearly identified by suitable ferrule',
    'All cable terminations should be done as per manufacturer installation instruction. Use correct crimping tool and De-Oxidation compound applied during terminations',
    'Check polarity of wires at termination ends in combiner box and Verify Continuity between +ve , -ve inputs and bus bar',
    'Check each series string of modules to be wired to its own circuit breaker or fuse as per drawing/SLD',
    'Check that conduits/hume pipes used for cable routing, should be secured at correct intervals as per IS /',
    'Check the Conduit bends used are uniform & free from kinks',
    'Check that proper conduit fittings used (i.e. Rain Tight at wet locations) and adequately tightened',
    'Check that conduit ends are closed and sealed as per specifications',
    'Check the combiner box is positioned in the upper area of the module, not to close to ground and in horizontal position with the wires hangs downwards. (Minimum clearance between ground to SCB shall be maintained as per approved drawings)',
    'Check if shed/canopy is required or not',
    'Check for the tightness & dressing of hanging wires from combiner box',
    'Check for HDPE conduit used for covering DC output power cable up to glanding and conduit open entry should be sealed.',
    'Check the IR Value& continuity for the DC Cables',
    'Check the combiner box sealed to cover any openings or leaks observed',
    'Combiner box should be cleaned out of all debris & vacuumed',
    'Combiner box should be installed with labels applicable as per drawings/specifications',
    'Combiner box grounded as per drawings/specifications',
    'Check for Earth strip sizes used and connections done as per approved drawing',
    'Check for - i) SCB mounting on a structural stable mounting frame Ii) Accessibility to SCB Iii) Check wire terminations tightness as per torque',
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist for AC DC Cables_R00_1-7-26.pdf  (HRPPL/ELE/I&C/CABLE)
# ONE document, TWO tasks. Its "DC Solar cable" half joins SCB/AJB on DC Cable
# Laying; its "AC Cable" half is the whole of AC Cable Laying. Split rather than
# linked to both tasks: a shared checklist would show the AC engineer eleven DC
# lines to ignore, and completion is per (item, task) so they would be answered
# twice.
# ---------------------------------------------------------------------------
SEC_DC_CABLE = 'DC Solar Cable'
DC_SOLAR_CABLE = [
    'Cable make approved as per approved BOM/design',
    'Cable size verified as per BOM',
    'UV resistant cable to be checked with test report',
    'Cable routing completed as per drawing',
    'Cable tagging/identification visual check',
    'Polarity check with multimeter/clamp meter',
    'Insulation resistance test with IR tester',
    'MC4 connectors crimped(must crimped with crimping tool not temporary /short cut/by other instruments also tightness to be veified, no loose connection/tightening to be observed)',
    'Cable tray/conduit support',
    'Earthing of metallic raceways/parts/points',
    'Cable tray/conduit support/fixers',
]

AC_CABLE = [
    'Cable size verified as per approved BOM/design',
    'Cable routing completed as per drawing',
    'Cable gland installation',
    'Cable termination(with proper lugging) and tightening',
    'Phase sequence check R, Y, B & Neutral',
    'Insulation resistance test with IR tester and recorded',
    'Continuity test with multimeter',
    'Cable identification tags visual',
    'Earthing continuity with E tester',
    'As-built documentation done(actual cable Inc data) for record',
    'Cable tray/conduit support/clamping to be checked',
]

# ---------------------------------------------------------------------------
# Source: I&C Check list for DCDB_R00_1-7-26.pdf  (HRPPL/ELE/I&C/DCDB)
# ---------------------------------------------------------------------------
SEC_DCDB = 'DCDB'
DCDB = [
    'Check DCDB Panel & Make as per approved drawings / specifications',
    'Check for physical damage of equipment',
    'Panel installed at correct location & identified with labels as per drawings/SLD',
    'Check proper grouting & coupling of panels',
    'Check for correct position & alignment of DB as per SLD/drawings',
    'check for clearances from all sides while installation of DB, as per approved drawing / IS standard',
    'Cables installed should be of correct type and routed in correct section as per drawing / SLD',
    'Cables supported & clamped, above/below the LT panel as per drawings/specifications',
    'Check the cable bending radius strictly as per IS / Specifications (Avoid sharp cable bends)',
    'Cables protected & secured from physical damage and at all sharp edges',
    'All cable terminations should be done as per manufacturer installation instruction. Use correct crimping tool and De-Oxidation compound applied during terminations',
    'Check for cable terminations so that it shall not create load/ stress on termination ends and Gland plates supporting it',
    'Cable terminations tightened as per manufacturer torque specifications (Using Nut-bolt & spring washers)',
    'Check for tightness of Power & Earth Busbar as per torque value specified by manufacturer',
    "Check for Gland plate fixing arrangement after the cable terminations shall be as per Manufacturer's guidelines/ Approved drawings",
    'Electrical connections/terminations should be clearly marked as per SLD/drawing',
    'Cables correctly labeled as per drawing/specifications',
    'Check for DB dressing with wires identified by ferruling',
    'Enclosure should be sealed to cover any openings or leaks observed',
    'Check Breaker / MCCB operation Open - Close check Continuity',
    'Check Indication & Meter connection properly as per design & scheme',
    'Enclosure cleaned out& vacuumed',
    'Touchup painting applied if required',
    'Check for Panel internal Components/ Accessories and Door earthing connections as per drawing',
    'Labels installed on enclosure as per drawing',
    'Panel earthing done as per drawings/specifications',
    'Check the IR Value of cable Phase to Earth, Phase to Neutral, Neutral to Earth values',
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist for LT Panel(ACDB)_R00_1-7-26.pdf  (HRPPL/ELE/I&C/LVP)
# ---------------------------------------------------------------------------
SEC_LT_PANEL = 'LT Panel / ACDB'
LT_PANEL = [
    'Check LT Panel(ACDB) & Make of all parts as per approved drawings / Specifications',
    'Check for physical damage of equipment',
    'Panel installed at correct location & identified with labels as per drawings/ SLD',
    'Check for clearances from all sides while installation of Panel, as per approved drawing / IS standard',
    'Check proper grouting & coupling of panel',
    'Check for correct position & alignment of LT panel, Breakers as per SLD/ drawings',
    'Cables installed should be of correct type and routed in correct section as per drawing / SLD',
    'Cables supported & clamped, above/below the LT panel as per drawings/ specifications',
    'Check the cable bending radius strictly as per IS / Specifications (Avoid sharp cable bends)',
    'Cables protected & secured from physical damage and at all sharp edges',
    'All cable terminations should be done as per manufacturer installation instruction. Use correct crimping tool and De-oxidation compound applied during terminations',
    'Check for cable terminations so that it shall not create load/ stress on termination ends and Gland plates supporting it',
    'Check for tightness of Power & Earth Busbar as per torque value specified by manufacturer',
    "Check for Gland plate fixing arrangement after the cable terminations shall be as per Manufacturer's guidelines/ Approved drawings",
    'Bus bars, Cable terminations tightened as per manufacturer torque specifications (Using Nut-bolt & spring washers)',
    'Electrical connections/ terminations should be clearly marked as per SLD/ drawing',
    'Check the cable tags, Phase colour coding, ferrules, busbar entry',
    'Cables correctly labelled as per drawing/ specifications',
    'Check for the adequate clearance in between bus bars as per drawing/Panel layout',
    'Check for partition provided between the panel compartments',
    'Check for the continuity & phase sequence of the cables',
    'Shrouding covers provided for front of live bus bars',
    'Hume pipes used for cable routing, should be secured at correct intervals as per IS / Drawings',
    'Check that proper pipe fittings used (i.e. Rain Tight at wet locations) and adequately tightened',
    'Check that hume pipe ends are closed & sealed as per specifications',
    'Enclosure should be sealed to cover any openings or leaks observed',
    'Enclosure cleaned out & vacuumed',
    'Touch up painting applied if required',
    'Check for Panel internal Components/ Accessories and Door earthing connections as per drawing',
    'Panel earthing done as per approved earthing layout',
    'All medium voltage equipment should be earthed by two separate & distinct connections with earth',
]

# ---------------------------------------------------------------------------
# Source: I&C Checklist  for Inverters_R00_1-7-26.pdf  (HRPPL/ELE/I&C/INV)
# Page 3 carries a note ("Follow all Instruction to open & close the ACB...") and a
# sign-off block only. The note is instruction to the installer, not a check, and
# is not seeded as an item.
# ---------------------------------------------------------------------------
INVERTERS = [
    'Check Inverter type, Rating & Make as per as per approved drawings / specifications/BOM',
    'Check for physical damage of equipment',
    'Inverter installed at correct location & identified with labels as per drawings/SLD',
    'Check for installation environment should be as per design/manufacturer instructions (i.e. Ambient Temp., Humidity, Vibrations, and Mechanical Shocks etc.)',
    'Check if shed/canopy with Inv stand is properly assembled or not',
    'Provide proper space for operation & maintenance in inverter installation room/location as per drawings',
    'All steel treated with rust protection',
    'Check equipment mounting steel base frame, structure supports installed as per approved layout/drawings',
    'Check position of inverter properly erected on the steel base inserted in concrete plinth & levelled on both axes by plumb and sprit level- For Proper alignment',
    'Check inverters properly bolted in floors or on wall with bracket and base angle supports and apply torque as per design/manufacturer instructions',
    'Cables installed should be of correct type and routed in correct section as per drawing / SLD',
    'Cables protected & secured from physical damage and at all sharp edges',
    'Cables arranged & supported as per drawings/specifications and check for gap between bus bars and lugs',
    'Check for Cable supports and its mounting arrangement as per approved drawings',
    'Check the cable bending radius strictly as per IS / Specifications (Avoid sharp cable bends) # solar cable 4-6 x OD(mm), multicore unarmoured cable 12xOD(mm), multicore armoured cable 15x OD',
    'All cable terminations should be done as per manufacturer installation instruction. Use correct crimping tool and De-Oxidation compound applied during terminations',
    'All Cable terminations tightened as per manufacturer torque specifications',
    'Electrical connections/terminations should be clearly marked as per SLD/drawing',
    'Cables correctly labelled as per drawing/specifications',
    'Check megger& continuity for all the incoming DC & Outgoing AC cable.',
    'Check the Polarities of DC, AC & Auxiliary power cables.',
    'Check the Tagging for all cables and ferruling for control cables.',
    'Check all meters & control wiring connected as per drawing',
    'Non-magnetic hardware used for all current connections',
    'Check for proper gland hole for input and output cable and ensure fitting and sealing on completion leaving no.',
    'Check that conduits/hume pipes used for cable routing, should be secured at correct intervals as per IS / Specifications/ Approved Drawings',
    'Check the Conduit bends used are uniform & free from kinks',
    'Check that proper conduit fittings used (i.e. Rain Tight at wet locations) and adequately tightened',
    'Check that conduit ends are closed and sealed as per specifications',
    'Enclosure should be sealed to cover any openings or leaks observed',
    'Enclosure cleaned out & vacuumed',
    'Warning & Arc flash labels installed on enclosure',
    'Check for Enclosure body earthing tightness and provided as per drawings/specifications',
    'Touch up painting applied if required',
]


def _flat(section, labels):
    """A single-source document's items, all under one section ('' = ungrouped)."""
    return [(section, label) for label in labels]


# THE SEVEN. `expected` is a transcription self-check, not decoration: if an item is ever
# dropped or duplicated while editing the lists above, the command refuses to seed rather
# than quietly writing a short checklist an engineer would sign off as complete.
CHECKLISTS = [
    {
        'code': 'OPEX-INST-CIVIL-MMS',
        'name': 'Civil Work and MMS Installation',
        'task_code': 'CIVIL_WORK_AND_MMS_INSTALLATION',
        'sources': 'HRPPL/CIVIL/BLOCK R00; HRPPL/ELE/STR R00',
        'items': _flat(SEC_BLOCK, CONCRETE_BLOCK) + STRUCTURE,
        'expected': 46,
    },
    {
        'code': 'OPEX-INST-MODULE',
        'name': 'Module Installation',
        'task_code': 'MODULE_INSTALLATION',
        'sources': 'HRPPL/ELE/I&C/MOD R00',
        'items': _flat('', PV_MODULES),
        'expected': 29,
    },
    {
        'code': 'OPEX-INST-LA-EARTHING',
        'name': 'LA and Earthing Installation',
        'task_code': 'LA_AND_EARTHING_INSTALLATION',
        'sources': 'HRPPL/ELE/I&C/EAR R00; HRPPL/ELE/I&C/ESE LA R00',
        'items': EARTHING + _flat(SEC_ESE_LA, ESE_LA),
        'expected': 54,
    },
    {
        'code': 'OPEX-INST-DC-CABLE',
        'name': 'DC Cable Laying with Conduit',
        'task_code': 'DC_CABLE_LAYING_WITH_CONDUIT',
        'sources': 'HRPPL/ELE/I&C/SCB-AJB R00; HRPPL/ELE/I&C/CABLE R00 (DC half)',
        'items': _flat(SEC_SCB, SCB_AJB) + _flat(SEC_DC_CABLE, DC_SOLAR_CABLE),
        'expected': 40,
    },
    {
        'code': 'OPEX-INST-DCDB-ACDB',
        'name': 'DCDB and ACDB Installation',
        'task_code': 'DCDB_AND_ACDB_INSTALLATION',
        'sources': 'HRPPL/ELE/I&C/DCDB R00; HRPPL/ELE/I&C/LVP R00',
        'items': _flat(SEC_DCDB, DCDB) + _flat(SEC_LT_PANEL, LT_PANEL),
        'expected': 58,
    },
    {
        'code': 'OPEX-INST-INVERTER',
        'name': 'Inverter Installation',
        'task_code': 'INVERTER_INSTALLATION',
        'sources': 'HRPPL/ELE/I&C/INV R00',
        'items': _flat('', INVERTERS),
        'expected': 34,
    },
    {
        'code': 'OPEX-INST-AC-CABLE',
        'name': 'AC Cable Laying',
        'task_code': 'AC_CABLE_LAYING',
        'sources': 'HRPPL/ELE/I&C/CABLE R00 (AC half)',
        'items': _flat('', AC_CABLE),
        'expected': 11,
    },
]

# Named so the report says WHY these two are absent rather than leaving a reader to
# notice that Phase 4 has nine tasks and seven checklists.
NOT_SEEDED = {
    'RMS_INSTALLATION': 'deferred to a later version (Scada-RMS document exists, not seeded)',
    'SOLAR_GENERATION_METER_INSTALLATION': 'no source document supplied',
}


class Command(BaseCommand):
    help = 'Seed the seven real HRPPL installation checklists onto the OPEX Installation phase.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created and write nothing.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        # Transcription self-check runs BEFORE anything is resolved or written, so a
        # miscount is reported as a content bug and not as a half-finished seed.
        for spec in CHECKLISTS:
            if len(spec['items']) != spec['expected']:
                raise CommandError(
                    f"{spec['code']}: transcription check failed - "
                    f"{len(spec['items'])} items built, {spec['expected']} expected."
                )

        template = TaskTemplate.objects.filter(
            project_type='OPEX', status=TaskTemplate.ACTIVE,
        ).first()
        if template is None:
            raise CommandError('No ACTIVE OPEX task template - nothing to link checklists to.')
        self.stdout.write(f'Active OPEX template: {template.label} v{template.version_no}')

        # Resolve every template task up front. A missing code means the template moved
        # under this content and a human must re-read the mapping, so it aborts rather
        # than seeding the six it could find.
        tasks = {}
        for spec in CHECKLISTS:
            tt = TaskTemplateTask.objects.filter(
                phase__template=template, code=spec['task_code'],
            ).first()
            if tt is None:
                raise CommandError(
                    f"Template task '{spec['task_code']}' not found in "
                    f'{template.label} v{template.version_no}.'
                )
            tasks[spec['task_code']] = tt

        created, skipped = [], []

        with transaction.atomic():
            for spec in CHECKLISTS:
                tt = tasks[spec['task_code']]

                existing = Checklist.objects.filter(code=spec['code']).first()
                if existing is not None:
                    skipped.append((
                        spec,
                        f'checklist code already exists (v{existing.version_no}, {existing.status})',
                    ))
                    continue

                clash = ChecklistTaskLink.objects.filter(
                    template_task__code=spec['task_code'],
                    template_task__phase__template__project_type='OPEX',
                ).first()
                if clash is not None:
                    skipped.append((spec, f'task already linked to checklist {clash.checklist_id}'))
                    continue

                if dry_run:
                    created.append((spec, None))
                    continue

                # Authored as a DRAFT and activated at the end, because ChecklistItem's
                # R-7 guard refuses writes to a non-draft parent. Same order the
                # portal-admin authoring screen uses.
                checklist = Checklist.objects.create(
                    code=spec['code'],
                    name=spec['name'],
                    version_no=1,
                    status=Checklist.DRAFT,
                    requires_photo=False,
                )
                ChecklistItem.objects.bulk_create([
                    ChecklistItem(checklist=checklist, label=label, section=section, order=n)
                    for n, (section, label) in enumerate(spec['items'], start=1)
                ])
                checklist.activate()

                # template_task is the key; save() derives task_name/project_type from it,
                # so the string columns can never disagree with the FK.
                ChecklistTaskLink.objects.create(checklist=checklist, template_task=tt)
                created.append((spec, checklist))

            if dry_run:
                transaction.set_rollback(True)

        verb  = 'Would create' if dry_run else 'Created'
        total = 0
        for spec, checklist in created:
            total += spec['expected']
            pk       = f' (pk {checklist.pk})' if checklist else ''
            sections = len({section for section, _ in spec['items']} - {''})
            self.stdout.write(self.style.SUCCESS(
                f"  {verb}: {spec['name']}{pk} - {spec['expected']} items, "
                f"{sections} sections -> {spec['task_code']}"
            ))
        for spec, why in skipped:
            self.stdout.write(self.style.WARNING(f"  Skipped: {spec['name']} - {why}"))

        for code, why in NOT_SEEDED.items():
            self.stdout.write(f'  Not seeded: {code} - {why}')

        self.stdout.write(self.style.SUCCESS(
            f'\n{verb.lower()} {len(created)} checklists, {total} items '
            f'({len(skipped)} skipped).'
        ))
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - nothing was written.'))
