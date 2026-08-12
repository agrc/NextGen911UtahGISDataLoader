import arcpy
import os
import time
from LoadNG911Helpers import (
    ensure_scratch_gdb,
    export_feature_class,
    truncate_dataset,
    get_domain_map,
    get_field_domain_name,
    get_coded_value_description,
    build_postal_comm_lookup,
    map_road_class,
    map_one_way,
    parse_esn,
    format_county_name
)

"""Load Roads into NG911 target RoadCenterlines and StreetNameAliasTable datasets."""

# Script configuration for direct runs or importing into other workflows.
TARGET_GDB = r"C:\temp\ng911_db.gdb"
SCRATCH_GDB = r"C:\temp\ng911scratch.gdb"
SGID = r"INSERT DATABASE CONNECTION PATH HERE"
SGID_ROADS = rf"{SGID}\SGID.TRANSPORTATION.Roads"
SGID_ZIPCODES = rf"{SGID}\SGID.BOUNDARIES.ZipCodes"
TRUNCATE = True
ERROR_LOG_PATH = os.path.join(os.path.dirname(SCRATCH_GDB), "LoadNG911Roads_errors.txt")

# Timing information for the script when executed directly.
start_time = time.time()
readable_start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
print(f"Script start time: {readable_start}")


def load_roads_and_aliases(target_gdb, scratch_gdb, postal_lookup, truncate=False):
    """Load road geometry and generate alias rows in the NG911 target geodatabase."""
    """Load road geometry and alias rows into NG911 target datasets."""
    src_fc = os.path.join(scratch_gdb, "Roads")
    tgt_fc = os.path.join(target_gdb, "RoadCenterlines")
    alias_table = os.path.join(target_gdb, "StreetNameAliasTable")

    # Validate required source and target datasets before opening cursors.
    if not arcpy.Exists(src_fc):
        raise ValueError(f"Scratch Roads feature class not found: {src_fc}")
    if not arcpy.Exists(tgt_fc):
        raise ValueError(f"Target RoadCenterlines feature class not found: {tgt_fc}")
    if not arcpy.Exists(alias_table):
        raise ValueError(f"Target StreetNameAliasTable not found: {alias_table}")

    if truncate:
        truncate_dataset(tgt_fc)
        truncate_dataset(alias_table)

    print("Loading RoadCenterlines and StreetNameAliasTable...")

    # Build the domain lookup once so each row can translate domain codes quickly.
    domain_map = get_domain_map(scratch_gdb)
    get_desc = get_coded_value_description
    format_county = format_county_name
    map_class = map_road_class
    map_oneway = map_one_way

    def is_valid_address_value(value):
        if value is None:
            return False
        if isinstance(value, str):
            value = value.strip()
            if value == "":
                return False
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return False

    def compute_parity_from_range(start_value, end_value):
        parities = set()
        for value in (start_value, end_value):
            if is_valid_address_value(value):
                number = int(str(value).strip())
                parities.add("E" if number % 2 == 0 else "O")
        if len(parities) == 2:
            return "B"
        if len(parities) == 1:
            return parities.pop()
        return None

    def normalize_parity(value):
        if value is None:
            return None
        normalized = str(value).strip().upper()
        if normalized in {"O", "E", "B"}:
            return normalized
        return None

    def is_null_or_empty(value):
        if value is None:
            return True
        if isinstance(value, str) and value.strip() == "":
            return True
        return False

    def fill_missing_side_values(left_value, right_value):
        if is_null_or_empty(left_value) and not is_null_or_empty(right_value):
            return right_value, right_value, 1
        if is_null_or_empty(right_value) and not is_null_or_empty(left_value):
            return left_value, left_value, 1
        return left_value, right_value, 0

    def to_str(value):
        return "" if value is None else str(value).strip()

    def fill_postal_values(postcode_l, postcode_r, postcomm_l, postcomm_r, postal_lookup):
        fill_count = 0

        if is_null_or_empty(postcode_l) and not is_null_or_empty(postcode_r):
            postcode_l = postcode_r
            fill_count += 1
        if is_null_or_empty(postcode_r) and not is_null_or_empty(postcode_l):
            postcode_r = postcode_l
            fill_count += 1

        if is_null_or_empty(postcomm_l) and not is_null_or_empty(postcomm_r):
            postcomm_l = postcomm_r
            fill_count += 1
        if is_null_or_empty(postcomm_r) and not is_null_or_empty(postcomm_l):
            postcomm_r = postcomm_l
            fill_count += 1

        if is_null_or_empty(postcomm_l) and not is_null_or_empty(postcode_l):
            postal_comm = postal_lookup.get(to_str(postcode_l), "")
            if postal_comm:
                postcomm_l = postal_comm
                fill_count += 1
        if is_null_or_empty(postcomm_r) and not is_null_or_empty(postcode_r):
            postal_comm = postal_lookup.get(to_str(postcode_r), "")
            if postal_comm:
                postcomm_r = postal_comm
                fill_count += 1

        # Attempt reverse lookup: if postcode is missing but postcomm exists,
        # try to find a ZIP that maps to the postal community name.
        try:
            postal_lookup_rev = {str(v).strip().upper(): k for k, v in postal_lookup.items() if v}
        except Exception:
            postal_lookup_rev = {}

        if is_null_or_empty(postcode_l) and not is_null_or_empty(postcomm_l):
            zip_from_name = postal_lookup_rev.get(to_str(postcomm_l).upper(), "")
            if zip_from_name:
                postcode_l = zip_from_name
                fill_count += 1
        if is_null_or_empty(postcode_r) and not is_null_or_empty(postcomm_r):
            zip_from_name = postal_lookup_rev.get(to_str(postcomm_r).upper(), "")
            if zip_from_name:
                postcode_r = zip_from_name
                fill_count += 1

        return postcode_l, postcode_r, postcomm_l, postcomm_r, fill_count

    def build_road_alias_rows(unique_id, objectid, source, updated, effective, expire,
                              rcl_nguid, source_row, domain_map, predir_domain, postdir_domain,
                              posttype_domain, a1_predir, a1_posttype, a1_postdir,
                              a2_predir, a2_posttype, a2_postdir, an_postdir,
                              an_name, a1_name, a2_name):
        alias_rows = []
        alias_count = 0
        SOURCE_PREDIR_INDEX = 28

        def build_alias(alias_type, alias_name, alias_postdir, alias_predir, alias_posttype):
            nonlocal alias_count
            if not alias_name:
                return

            alias_count += 1
            a_st_name = alias_name
            a_st_posdir = get_coded_value_description(domain_map, postdir_domain, alias_postdir).upper() if alias_postdir else ""
            if alias_type == "AN":
                a_st_predir = get_coded_value_description(domain_map, predir_domain, source_row[SOURCE_PREDIR_INDEX]).upper()
                a_st_posttype = ""
            else:
                if alias_predir:
                    a_st_predir = get_coded_value_description(domain_map, predir_domain, alias_predir).upper()
                else:
                    a_st_predir = get_coded_value_description(domain_map, predir_domain, source_row[SOURCE_PREDIR_INDEX]).upper()
                a_st_posttype = get_coded_value_description(domain_map, posttype_domain, alias_posttype).upper() if alias_posttype else ""

            alias_rows.append([
                a_st_name,
                source,
                updated,
                effective,
                expire,
                rcl_nguid,
                f"{unique_id}|{alias_count}",
                a_st_posdir,
                a_st_predir,
                a_st_posttype
            ])

        build_alias("AN", an_name, an_postdir, None, None)
        build_alias("A1", a1_name, a1_postdir, a1_predir, a1_posttype)
        build_alias("A2", a2_name, a2_postdir, a2_predir, a2_posttype)
        return alias_rows

    # Source and target field lists must match the schema exactly, including the order used by cursors.
    # The search_fields array defines the incoming scratch Roads schema.
    search_fields = [
        "SHAPE@",      # 0
        "SOURCE",      # 1
        "NAME",        # 2
        "UPDATED",     # 3
        "FROMADDR_L",  # 4
        "TOADDR_L",    # 5
        "FROMADDR_R",  # 6
        "TOADDR_R",    # 7
        "EFFECTIVE",   # 8
        "EXPIRE",      # 9
        "GlobalID",    # 10
        "PARITY_L",    # 11
        "PARITY_R",    # 12
        "ESN_L",       # 13
        "ESN_R",       # 14
        "POSTCOMM_L",  # 15
        "POSTCOMM_R",  # 16
        "STATE_L",     # 17
        "STATE_R",     # 18
        "INCMUNI_L",   # 19
        "INCMUNI_R",   # 20
        "UNINCCOM_L",  # 21
        "UNINCCOM_R",  # 22
        "NBRHDCOM_L",  # 23
        "NBRHDCOM_R",  # 24
        "ZIPCODE_L",   # 25
        "ZIPCODE_R",   # 26
        "SPEED_LMT",   # 27
        "PREDIR",      # 28
        "POSTTYPE",    # 29
        "POSTDIR",     # 30
        "CARTOCODE",   # 31
        "ONEWAY",      # 32
        "COUNTY_L",    # 33
        "COUNTY_R",    # 34
        "UNIQUE_ID",   # 35
        "OBJECTID",    # 36
        "AN_NAME",     # 37
        "A1_NAME",     # 38
        "A2_NAME",     # 39
        "AN_POSTDIR",  # 40
        "A1_POSTTYPE", # 41
        "A2_POSTTYPE", # 42
        "A1_PREDIR",   # 43
        "A2_PREDIR",   # 44
        "A1_POSTDIR",  # 45
        "A2_POSTDIR",  # 46
        "FULLNAME",    # 47
    ]

    # The road_insert_fields order must match the target RoadCenterlines schema.
    road_insert_fields = [
        "SHAPE@",      # 0
        "Source",      # 1
        "StreetName",  # 2
        "DateUpdated", # 3
        "FromAddr_L",  # 4
        "ToAddr_L",    # 5
        "FromAddr_R",  # 6
        "ToAddr_R",    # 7
        "Effective",   # 8
        "Expire",      # 9
        "RCL_NGUID",   # 10
        "Parity_L",    # 11
        "Parity_R",    # 12
        "ESN_L",       # 13
        "ESN_R",       # 14
        "MSAGComm_L",  # 15
        "MSAGComm_R",  # 16
        "Country_L",   # 17
        "Country_R",   # 18
        "State_L",     # 19
        "State_R",     # 20
        "IncMuni_L",   # 21
        "IncMuni_R",   # 22
        "UnincCom_L",  # 23
        "UnincCom_R",  # 24
        "NbrhdCom_L",  # 25
        "NbrhdCom_R",  # 26
        "PostCode_L",  # 27
        "PostCode_R",  # 28
        "PostComm_L",  # 29
        "PostComm_R",  # 30
        "SpeedLimit",  # 31
        "LSt_PreDir",  # 32
        "LSt_Name",    # 33
        "LSt_Type",    # 34
        "LStPosDir",   # 35
        "RoadClass",   # 36
        "OneWay",      # 37
        "St_PosTyp",   # 38
        "St_Predir",   # 39
        "St_PosDir",   # 40
        "County_L",    # 41
        "County_R",    # 42
        "DiscrpAgID"   # 43
    ]

    # Alias insert fields define the StreetNameAliasTable output schema.
    alias_insert_fields = [
        "ASt_Name",
        "Source",
        "DateUpdate",
        "Effective",
        "Expire",
        "RCL_NGUID",
        "ASt_NGUID",
        "ASt_PosDir",
        "ASt_PreDir",
        "AStPosType"
    ]

    county_l_domain = get_field_domain_name(src_fc, "COUNTY_L")
    county_r_domain = get_field_domain_name(src_fc, "COUNTY_R")
    posttype_domain = get_field_domain_name(src_fc, "POSTTYPE")
    predir_domain = get_field_domain_name(src_fc, "PREDIR")
    postdir_domain = get_field_domain_name(src_fc, "POSTDIR")

    inserted_rows = 0
    alias_counter = 0
    skipped_segments = 0
    error_count = 0
    error_log_entries = []
    alias_rows_to_insert = []
    qc_fill_count = 0

    with arcpy.da.SearchCursor(src_fc, search_fields) as search_cursor, \
         arcpy.da.InsertCursor(tgt_fc, road_insert_fields) as road_insert_cursor:
        for row in search_cursor:
            # Unpack row values by position to keep cursor code readable and stable.
            shape = row[0]
            source = row[1]
            name = row[2]
            updated = row[3]
            from_addr_l = row[4]
            to_addr_l = row[5]
            from_addr_r = row[6]
            to_addr_r = row[7]
            effective = row[8]
            expire = row[9]
            global_id = row[10]
            parity_l = row[11]
            parity_r = row[12]
            esn_l = parse_esn(row[13])
            esn_r = parse_esn(row[14])
            postcomm_l = row[15]
            postcomm_r = row[16]
            state_l = row[17]
            state_r = row[18]
            incmuni_l = row[19]
            incmuni_r = row[20]
            unincccom_l = row[21]
            unincccom_r = row[22]
            nbrhdcom_l = row[23]
            nbrhdcom_r = row[24]
            zipcode_l = row[25]
            zipcode_r = row[26]
            speed_limit = row[27]
            predir = row[28]
            posttype = row[29]
            postdir = row[30]
            cartocode = row[31]
            oneway = row[32]
            county_l = row[33]
            county_r = row[34]
            unique_id = row[35]
            objectid = row[36]
            an_name = row[37]
            a1_name = row[38]
            a2_name = row[39]
            an_postdir = row[40]
            a1_posttype = row[41]
            a2_posttype = row[42]
            a1_predir = row[43]
            a2_predir = row[44]
            a1_postdir = row[45]
            a2_postdir = row[46]
            fullname = row[47]

            # Create a stable NGUID using GlobalID if available, otherwise fallback to unique/object id combo.
            rcl_nguid = global_id if global_id is not None else f"{unique_id}|{objectid}"

            # Skip segments that have no valid left or right address range.
            has_left_range = is_valid_address_value(from_addr_l) or is_valid_address_value(to_addr_l)
            has_right_range = is_valid_address_value(from_addr_r) or is_valid_address_value(to_addr_r)
            if not (has_left_range or has_right_range):
                skipped_segments += 1
                continue

            parity_l = normalize_parity(parity_l) or compute_parity_from_range(from_addr_l, to_addr_l)
            parity_r = normalize_parity(parity_r) or compute_parity_from_range(from_addr_r, to_addr_r)

            if is_null_or_empty(state_l):
                state_l = "UT"
            if is_null_or_empty(state_r):
                state_r = "UT"

            postcomm_l, postcomm_r, fill_count = fill_missing_side_values(postcomm_l, postcomm_r)
            qc_fill_count += fill_count
            county_l, county_r, fill_count = fill_missing_side_values(county_l, county_r)
            qc_fill_count += fill_count
            zipcode_l, zipcode_r, fill_count = fill_missing_side_values(zipcode_l, zipcode_r)
            qc_fill_count += fill_count
            zipcode_l, zipcode_r, postcomm_l, postcomm_r, fill_count = fill_postal_values(
                zipcode_l,
                zipcode_r,
                postcomm_l,
                postcomm_r,
                postal_lookup
            )
            qc_fill_count += fill_count

            # Use local helper alias bindings for improved loop performance.
            roadclass = map_class(cartocode)
            one_way = map_oneway(oneway)
            st_postyp = get_desc(domain_map, posttype_domain, posttype).upper()
            st_predir = get_desc(domain_map, predir_domain, predir).upper()
            st_posdir = get_desc(domain_map, postdir_domain, postdir).upper()
            county_l_name = format_county(get_desc(domain_map, county_l_domain, county_l))
            county_r_name = format_county(get_desc(domain_map, county_r_domain, county_r))

            road_row = [
                shape,
                source,
                name,
                updated,
                from_addr_l,
                to_addr_l,
                from_addr_r,
                to_addr_r,
                effective,
                expire,
                rcl_nguid,
                parity_l,
                parity_r,
                esn_l,
                esn_r,
                postcomm_l,
                postcomm_r,
                "US",
                "US",
                state_l,
                state_r,
                incmuni_l,
                incmuni_r,
                unincccom_l,
                unincccom_r,
                nbrhdcom_l,
                nbrhdcom_r,
                zipcode_l,
                zipcode_r,
                postcomm_l,
                postcomm_r,
                speed_limit,
                predir,
                name,
                posttype,
                postdir,
                roadclass,
                one_way,
                st_postyp,
                st_predir,
                st_posdir,
                county_l_name,
                county_r_name,
                "https://gis.utah.gov/solutions/for-emergency-response"
            ]

            source_name = f"{postcomm_l} | {predir} {fullname}"
            try:
                road_insert_cursor.insertRow(road_row)
                inserted_rows += 1

                alias_rows = build_road_alias_rows(
                    unique_id,
                    objectid,
                    source,
                    updated,
                    effective,
                    expire,
                    rcl_nguid,
                    row,
                    domain_map,
                    predir_domain,
                    postdir_domain,
                    posttype_domain,
                    a1_predir,
                    a1_posttype,
                    a1_postdir,
                    a2_predir,
                    a2_posttype,
                    a2_postdir,
                    an_postdir,
                    an_name,
                    a1_name,
                    a2_name
                )
                alias_counter += len(alias_rows)
                alias_rows_to_insert.extend(alias_rows)

                if inserted_rows % 25000 == 0:
                    print(f"Inserted {inserted_rows} RoadCenterlines rows...")
            except RuntimeError as error:
                error_count += 1
                error_log_entries.append(f"{objectid}\t{unique_id}\t{source_name}\t{error}")
                continue

        print(f"RoadCenterlines load complete. {inserted_rows} rows inserted.")

    if alias_rows_to_insert:
        with arcpy.da.InsertCursor(alias_table, alias_insert_fields) as alias_insert_cursor:
            for alias_row in alias_rows_to_insert:
                alias_insert_cursor.insertRow(alias_row)
        print(f"StreetNameAliasTable insert complete. {alias_counter} alias rows created.")
    else:
        print("StreetNameAliasTable insert complete. 0 alias rows created.")

    if error_log_entries:
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as error_log:
            error_log.write("OBJECTID\tUNIQUE_ID\tROAD_INFO\tERROR_MESSAGE\n")
            error_log.write("\n".join(error_log_entries) + "\n")
        print(f"Error log written to: {ERROR_LOG_PATH}")
    print(f"Encountered {error_count} errors during Roads load.")
    print(f"Skipped {skipped_segments} road segments due to invalid address ranges.")
    if qc_fill_count:
        print(f"QC fill applied to {qc_fill_count} side values for MSAGComm, County, or PostCode fields.")

    #: Append AZ Roads
    print("Appending AZ Roads into RoadCenterlines...")
    arcpy.management.Append(inputs="//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs", target=rf"{TARGET_GDB}\RoadCenterlines", schema_type="NO_TEST", field_mapping='Source "Source of Data" true true false 75 Text 0 0 ,First,#;DateUpdated "Date Updated" true true false 8 Date 0 0 ,First,#;Effective "Effective Date" true true false 8 Date 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Effective,-1,-1;Expire "Expiration Date" true true false 8 Date 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Expire,-1,-1;RCL_NGUID "Road Centerline NENA Globally Unique ID" true true false 254 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,RCL_NGUID,-1,-1;AdNumPre_L "Left Address Number Prefix" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,AdNumPre_L,-1,-1;AdNumPre_R "Right Address Number Prefix" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,AdNumPre_R,-1,-1;FromAddr_L "Left FROM Address" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,FromAddr_L,-1,-1;ToAddr_L "Left TO Address" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,ToAddr_L,-1,-1;FromAddr_R "Right FROM Address" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,FromAddr_R,-1,-1;ToAddr_R "Right TO Address" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,ToAddr_R,-1,-1;Parity_L "Parity Left" true true false 1 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Parity_L,-1,-1;Parity_R "Parity Right" true true false 1 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Parity_R,-1,-1;St_PreMod "Street Name Pre Modifier" true true false 9 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PreMod,-1,-1;St_PreDir "Street Name Pre Directional" true true false 50 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PreDir,-1,-1;St_PreTyp "Street Name Pre Type" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PreTyp,-1,-1;St_PreSep "Street Name Pre Type Separator" true true false 20 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PreSep,-1,-1;StreetName "Street Name" true true false 60 Text 0 0 ,First,#;St_PosTyp "Street Name Post Type" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PosTyp,-1,-1;St_PosDir "Street Name Post Directional" true true false 9 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PosDir,-1,-1;St_PosMod "Street Name Post Modifier" true true false 25 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,St_PosMod,-1,-1;LSt_PreDir "Legacy Street Name Pre Directional" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,LSt_PreDir,-1,-1;LSt_Name "Legacy Street Name" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,LSt_Name,-1,-1;LSt_Type "Legacy Street Name Type" true true false 5 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,LSt_Type,-1,-1;LStPosDir "Legacy Street Name Post Directional" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,LStPosDir,-1,-1;ESN_L "ESN Left" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,ESN_L,-1,-1;ESN_R "ESN Right" true true false 4 Long 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,ESN_R,-1,-1;MSAGComm_L "MSAG Community Name Left" true true false 30 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,MSAGComm_L,-1,-1;MSAGComm_R "MSAG Community Name Right" true true false 30 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,MSAGComm_R,-1,-1;Country_L "Country Left" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Country_L,-1,-1;Country_R "Country Right" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Country_R,-1,-1;State_L "State Left" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,State_L,-1,-1;State_R "State Right" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,State_R,-1,-1;County_L "County Left" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,County_L,-1,-1;County_R "County Right" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,County_R,-1,-1;AddCode_L "Additional Code Left" true true false 6 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,AddCode_L,-1,-1;AddCode_R "Additional Code Right" true true false 6 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,AddCode_R,-1,-1;IncMuni_L "Incorporated Municipality Left" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,IncMuni_L,-1,-1;IncMuni_R "Incorporated Municipality Right" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,IncMuni_R,-1,-1;UnincCom_L "Unincorporated Community Left" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,UnincCom_L,-1,-1;UnincCom_R "Unincorporated Community Right" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,UnincCom_R,-1,-1;NbrhdCom_L "Neighborhood Community Left" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,NbrhdCom_L,-1,-1;NbrhdCom_R "Neighborhood Community Right" true true false 100 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,NbrhdCom_R,-1,-1;PostCode_L "Postal Code Left" true true false 7 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,PostCode_L,-1,-1;PostCode_R "Postal Code Right" true true false 7 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,PostCode_R,-1,-1;PostComm_L "Postal Community Name Left" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,PostComm_L,-1,-1;PostComm_R "Postal Community Name Right" true true false 40 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,PostComm_R,-1,-1;RoadClass "Road Class" true true false 15 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,RoadClass,-1,-1;OneWay "One-Way" true true false 2 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,OneWay,-1,-1;SpeedLimit "Speed Limit" true true false 2 Short 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,SpeedLimit,-1,-1;DiscrpAgID "Discrepancy Agency ID" true true false 75 Text 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,DiscrpAgID,-1,-1;dmNotesXML "dmNotesXML" true true false 3999 Text 0 0 ,First,#;dmValidation "dmValidation" true true false 3999 Text 0 0 ,First,#;dmClientUploadedFileID "dmClientUploadedFileID" true true false 4 Long 0 0 ,First,#;Shape_Length "Shape_Length" false true true 8 Double 0 0 ,First,#,//itwfpcap2/AGRC/agrc/data/ng911/UT_AZ_Border_PSAPs.gdb/AZNG911_RCL_in_Utah_PSAPs,Shape_Length,-1,-1', subtype="")



# If this module is imported, external code can call load_roads_and_aliases directly.
# Script-level configuration is defined at the top of the file so it is easy to override.

ensure_scratch_gdb(SCRATCH_GDB)
export_feature_class(SGID_ROADS, SCRATCH_GDB, "Roads")
postal_lookup = build_postal_comm_lookup(SGID_ZIPCODES)
load_roads_and_aliases(TARGET_GDB, SCRATCH_GDB, postal_lookup, TRUNCATE)


print("Script shutting down ...")
# Stop timer and print end time in local
readable_end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
print(f"Script end time: {readable_end}")
print(f"Time elapsed: {time.time() - start_time:.2f}s")
