import arcpy
import os
import re
import time
from LoadNG911Helpers import (
    ensure_scratch_gdb,
    export_feature_class,
    truncate_dataset,
    get_domain_map,
    get_field_domain_name,
    get_coded_value_description,
    build_postal_comm_lookup,
    map_pt_type,
    map_pt_location,
    map_direction,
    format_county_name
)

# Script timing information for logging and troubleshooting.
start_time = time.time()
readable_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
print("Script start time: {}".format(readable_start))


# Target geodatabase and scratch scratch workspace paths.
# These values can be adjusted for testing or production as needed.
TARGET_GDB = r"C:/temp/ng911_db.gdb"
SCRATCH_GDB = r"C:/temp/ng911scratch.gdb"
SGID = r"INSERT DATABASE CONNECTION PATH HERE" 
SGID_ADDRESSPOINTS = rf"{SGID}\SGID.LOCATION.AddressPoints"
SGID_ZIPCODES = rf"{SGID}\SGID.BOUNDARIES.ZipCodes"
TRUNCATE = True
ERROR_LOG_PATH = os.path.join(os.path.dirname(SCRATCH_GDB), "LoadNG911AddressPoints_errors.txt")


def load_address_points(target_gdb, scratch_gdb, postal_lookup, truncate=False):
    src_fc = os.path.join(scratch_gdb, "AddressPoints")
    tgt_fc = os.path.join(target_gdb, "AddressPoints")

    error_count = 0
    error_log_entries = []

    if not arcpy.Exists(src_fc):
        raise ValueError(f"Scratch AddressPoints feature class not found: {src_fc}")
    if not arcpy.Exists(tgt_fc):
        raise ValueError(f"Target AddressPoints feature class not found: {tgt_fc}")

    if truncate:
        truncate_dataset(tgt_fc)

    print("Loading AddressPoints...")

    # Build helper lookups once, outside the row loop.
    domain_map = get_domain_map(scratch_gdb)
    county_domain = get_field_domain_name(src_fc, "CountyID")
    street_type_domain = get_field_domain_name(src_fc, "StreetType")
    wgs84_spatial_ref = arcpy.SpatialReference(4326)

    # Field lists must match the source and target schema exactly.
    # Use indexed field positions consistently inside the row loop.
    search_fields = [
        "SHAPE@",      # 0
        "OID@",        # 1
        "AddSource",   # 2
        "LoadDate",    # 3
        "UTAddPtID",   # 4
        "City",        # 5
        "AddNum",      # 6
        "AddNumSuffix",# 7
        "StreetName",  # 8
        "PrefixDir",   # 9
        "SuffixDir",   # 10
        "ZipCode",     # 11
        "Building",    # 12
        "UnitID",      # 13
        "LandmarkName",# 14
        "PtType",      # 15
        "PtLocation",  # 16
        "CountyID",    # 17
        "StreetType"   # 18
    ]

    insert_fields = [
        "SHAPE@",      # 0
        "Source",      # 1
        "DateUpdate",  # 2
        "Site_NGUID",  # 3
        "Country",     # 4
        "State",       # 5
        "Inc_Muni",    # 6
        "Uninc_Comm",  # 7
        "Add_Number",  # 8
        "AddNum_Suf",  # 9
        "StreetName",  # 10
        "LSt_PreDir",  # 11
        "LSt_Name",    # 12
        "LSt_Type",    # 13
        "LStPosDir",   # 14
        "ESN",         # 15
        "Post_Code",   # 16
        "Building",    # 17
        "Unit",        # 18
        "LandmkName",  # 19
        "Long",        # 20
        "Lat",         # 21
        "Post_Comm",   # 22
        "MSAGComm",    # 23
        "Place_Type",  # 24
        "Placement",   # 25
        "County",      # 26
        "St_PosTyp",   # 27
        "St_PreDir",   # 28
        "St_PosDir",   # 29
        "DiscrpAgID"   # 30
    ]

    # Determine the destination field type so we can insert values in a compatible format.
    add_number_field = arcpy.ListFields(tgt_fc, "Add_Number")
    add_number_type = add_number_field[0].type if add_number_field else None
    integer_add_number_types = ("SmallInteger", "Integer")

    def normalize_add_number(raw_value, field_type, source_oid=None):
        """Normalize AddNum values for the target Add_Number field.

        This helper handles:
        - null/empty input
        - decimal ".5" values by stripping the decimal and returning a 1/2 suffix
        - removal of any nondigit characters
        - integer conversion for integer target fields
        """
        if raw_value is None:
            return (None, None) if field_type in integer_add_number_types else ("", None)

        raw_text = str(raw_value).strip()
        if raw_text == "":
            return (None, None) if field_type in integer_add_number_types else ("", None)

        fraction_suffix = None
        decimal_half_match = re.match(r"^\s*([0-9]+)\s*\.\s*5\s*$", raw_text)
        if decimal_half_match:
            raw_text = decimal_half_match.group(1)
            fraction_suffix = "1/2"

        cleaned = re.sub(r"[^0-9]", "", raw_text)
        if cleaned != raw_text:
            print(f"AddressPoints {source_oid or 'unknown'}: normalized AddNum '{raw_text}' -> '{cleaned}'")
        raw_text = cleaned

        if raw_text == "":
            return (None, fraction_suffix) if field_type in integer_add_number_types else ("", fraction_suffix)

        if field_type in integer_add_number_types:
            try:
                return int(raw_text), fraction_suffix
            except ValueError:
                return None, fraction_suffix

        # Default to string target fields for any remaining values.
        return raw_text, fraction_suffix

    with arcpy.da.SearchCursor(src_fc, search_fields) as search_cursor, \
         arcpy.da.InsertCursor(tgt_fc, insert_fields) as insert_cursor:
        row_count = 0
        for row in search_cursor:
            shape = row[0]
            source_oid = row[1]
            site_nguid = str(row[4]).strip() if row[4] is not None else ""
            if shape is None:
                print(f"Skipping AddressPoint OBJECTID {source_oid}: null geometry")
                continue

            try:
                source = row[2]
                date_update = row[3]
                city = row[5] if row[5] is not None else ""
                add_num_raw = str(row[6]).strip() if row[6] is not None else ""
                add_num_suffix = row[7] if row[7] is not None else ""
                street_name = row[8] if row[8] is not None else ""
                prefix_dir = row[9] if row[9] is not None else ""
                suffix_dir = row[10] if row[10] is not None else ""
                zipcode = str(row[11]).strip() if row[11] is not None else ""
                building = row[12] if row[12] is not None else ""
                unit_id = row[13] if row[13] is not None else ""
                landmark_name = row[14] if row[14] is not None else ""
                pt_type = row[15] if row[15] is not None else ""
                pt_location = row[16] if row[16] is not None else ""
                county_id = row[17] if row[17] is not None else None
                street_type = row[18] if row[18] is not None else None

                add_number, decimal_suffix = normalize_add_number(row[6], add_number_type, source_oid)
                if decimal_suffix:
                    add_num_suffix = decimal_suffix

                if source_oid == 298:
                    print(f"Debug: AddressPoint OBJECTID 298 - AddNum raw: '{row[6]}', normalized: '{add_number}'")
                    print(f"need add_number_type: {add_number_type}")
                    print(type(add_number))

                # Reproject geometry to WGS84 if possible; keep original geometry on failure.
                projected_shape = shape
                if hasattr(shape, "projectAs"):
                    try:
                        projected_shape = shape.projectAs(wgs84_spatial_ref)
                    except Exception:
                        projected_shape = shape

                long_val = None
                lat_val = None
                try:
                    long_val = projected_shape.centroid.X
                    lat_val = projected_shape.centroid.Y
                except Exception:
                    long_val = None
                    lat_val = None

                postal_comm = ""
                if zipcode:
                    postal_comm = postal_lookup.get(zipcode, "")

                place_type = map_pt_type(pt_type)
                placement = map_pt_location(pt_location)

                # Convert coded domains to the actual string values expected by the target schema.
                county_description = get_coded_value_description(domain_map, county_domain, county_id)
                county = format_county_name(county_description)

                st_postyp = get_coded_value_description(domain_map, street_type_domain, street_type).upper()
                st_predir = map_direction(prefix_dir)
                st_posdir = map_direction(suffix_dir)

                insert_row = [
                    projected_shape,
                    source,
                    date_update,
                    site_nguid,
                    "US",
                    "UT",
                    city,
                    "",
                    add_number,
                    add_num_suffix,
                    street_name,
                    prefix_dir,
                    street_name,
                    street_type,
                    suffix_dir,
                    "0",
                    zipcode,
                    building,
                    unit_id,
                    landmark_name,
                    long_val,
                    lat_val,
                    postal_comm,
                    postal_comm,
                    place_type,
                    placement,
                    county,
                    st_postyp,
                    st_predir,
                    st_posdir,
                    "https://gis.utah.gov/solutions/for-emergency-response"
                ]

                insert_cursor.insertRow(insert_row)
                row_count += 1
                if row_count % 50000 == 0:
                    print(f"Inserted {row_count} AddressPoints rows...")

            except RuntimeError as error:
                error_count += 1
                error_log_entries.append(f"{source_oid}\t{site_nguid}\t{error}")
                # print(f"Runtime error processing AddressPoint OBJECTID {source_oid}, Site_NGUID '{site_nguid}': {error}")
                continue

        print(f"AddressPoints load complete. {row_count} rows inserted.")
        if error_log_entries:
            with open(ERROR_LOG_PATH, "a", encoding="utf-8") as error_log:
                error_log.write("SOURCE_OID\tSITE_NGUID\tERROR_MESSAGE\n")
                error_log.write("\n".join(error_log_entries) + "\n")
            print(f"Error log written to: {ERROR_LOG_PATH}")
        print(f"encountered {error_count} errors during AddressPoints load.")

    #: Append AZ Address Points
    print("Appending AZ Address Points to target AddressPoints feature class...")
    arcpy.management.Append(inputs="//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs", target=rf"{TARGET_GDB}\AddressPoints", schema_type="NO_TEST", field_mapping='Source "Source of Data" true true false 75 Text 0 0 ,First,#;DateUpdate "Date Update" true true false 8 Date 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,DateUpdate,-1,-1;Effective "Effective Date" true true false 8 Date 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Effective,-1,-1;Expire "Expiration Date" true true false 8 Date 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Expire,-1,-1;Site_NGUID "Site NENA Globally Unique ID" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Site_NGUID,-1,-1;Country "Country" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Country,-1,-1;State "State" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,State,-1,-1;County "County" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,County,-1,-1;AddCode "Additional Code" true true false 6 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,AddCode,-1,-1;AddDataURI "Additional Data URI" true true false 254 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,AddDataURI,-1,-1;Inc_Muni "Incorporated Municipality" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Inc_Muni,-1,-1;Uninc_Comm "Unincorporated Community" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Uninc_Comm,-1,-1;Nbrhd_Comm "Neighborhood Community" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Nbrhd_Comm,-1,-1;AddNum_Pre "Address Number Prefix" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,AddNum_Pre,-1,-1;Add_Number "Address Number" true true false 2 Short 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Add_Number,-1,-1;AddNum_Suf "Address Number Suffix" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,AddNum_Suf,-1,-1;St_PreMod "Street Name Pre Modifier" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PreMod,-1,-1;St_PreDir "Street Name Pre Directional" true true false 9 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PreDir,-1,-1;St_PreTyp "Street Name Pre Type" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PreTyp,-1,-1;St_PreSep "Street Name Pre Type Separator" true true false 20 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PreSep,-1,-1;StreetName "Street Name" true true false 60 Text 0 0 ,First,#;St_PosTyp "Street Name Post Type" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PosTyp,-1,-1;St_PosDir "Street Name Post Directional" true true false 9 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PosDir,-1,-1;St_PosMod "Street Name Post Modifier" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,St_PosMod,-1,-1;LSt_PreDir "Legacy Street Name Pre Directional" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,LSt_PreDir,-1,-1;LSt_Name "Legacy Street Name" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,LSt_Name,-1,-1;LSt_Type "Legacy Street Name Type" true true false 5 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,LSt_Type,-1,-1;LStPosDir "Legacy Street Name Post Directional" true true false 2 Text 0 0 ,First,#;ESN "ESN" true true false 0 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,ESN,-1,-1;MSAGComm "MSAG Community Name" true true false 30 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,MSAGComm,-1,-1;Post_Comm "Postal Community Name" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Post_Comm,-1,-1;Post_Code "Postal Code" true true false 7 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Post_Code,-1,-1;Post_Code4 "ZIP Plus 4" true true false 4 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Post_Code4,-1,-1;Building "Building" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Building,-1,-1;Floor "Floor" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Floor,-1,-1;Unit "Unit" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Unit,-1,-1;Room "Room" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Room,-1,-1;Seat "Seat" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Seat,-1,-1;Addtl_Loc "Additional Location Information" true true false 225 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Addtl_Loc,-1,-1;LandmkName "Complete Landmark Name" true true false 150 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,LandmkName,-1,-1;Mile_Post "Mile Post" true true false 150 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Mile_Post,-1,-1;Place_Type "Place Type" true true false 50 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Place_Type,-1,-1;Placement "Placement Method" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Placement,-1,-1;Long "Longitude" true true false 4 Float 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Long,-1,-1;Lat "Latitude" true true false 4 Float 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Lat,-1,-1;Elev "Elevation" true true false 2 Short 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,Elev,-1,-1;DiscrpAgID "Discrepancy Agency ID" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_SSAP_in_Utah_PSAPs,DiscrpAgID,-1,-1;GlobalID "GlobalID" false false true 38 GlobalID 0 0 ,First,#', subtype="")




ensure_scratch_gdb(SCRATCH_GDB)
export_feature_class(SGID_ADDRESSPOINTS, SCRATCH_GDB, "AddressPoints")
postal_lookup = build_postal_comm_lookup(SGID_ZIPCODES)
load_address_points(TARGET_GDB, SCRATCH_GDB, postal_lookup, TRUNCATE)


print("Script shutting down ...")
# Stop timer and print end time in local
readable_end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
print("The script end time is {}".format(readable_end))
print("Time elapsed: {:.2f}s".format(time.time() - start_time))
