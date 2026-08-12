"""Shared helper functions and lookup mappings for the NG911 ETL scripts."""

import os
import re
import arcpy

# Allow outputs to be overwritten during geoprocessing runs.
arcpy.env.overwriteOutput = True

# Mapping dictionaries translate source codes and descriptions into NG911 target values.
# Keeping them at module scope avoids recreating them for each row processed.
PT_TYPE_MAP = {
    "Agricultural": "",
    "BASE ADDRESS": "Government-base",
    "Business": "",
    "Commercial": "",
    "Education": "School",
    "Government": "Government",
    "Industrial": "Industrial",
    "Med": "Hospital",
    "Mixed Use": "",
    "OTH": "Other",
    "Other": "Other",
    "Residential": "Residence",
    "Unknown": "Unknown",
    "Vacant": ""
}

PT_LOCATION_MAP = {
    "Centroid": "",
    "Driveway Entrance": "Property Access",
    "Geocoded": "Geocoding",
    "Other": "",
    "Parcel Centroid": "Parcel",
    "Primary Structure Entrance": "Structure",
    "Residential": "Site",
    "Rooftop": "",
    "Unknown": "Unknown"
}

DIRECTION_MAP = {
    "N": "NORTH",
    "S": "SOUTH",
    "E": "EAST",
    "W": "WEST"
}

ROAD_CLASS_MAP = {
    1: "Primary",
    2: "Primary",
    3: "Secondary",
    4: "Primary",
    5: "Secondary",
    6: "Secondary",
    7: "Ramp",
    8: "Local",
    9: "Local",
    10: "Local",
    11: "Local",
    12: "Other",
    13: "Other",
    14: "Private",
    15: "",
    16: "Vehicular Trail",
    17: "Other",
    18: "Other"
}

ONE_WAY_MAP = {
    "0": "B",
    "1": "FT",
    "2": "TF"
}




def ensure_scratch_gdb(scratch_gdb_path):
    """Ensure a scratch file geodatabase exists for temporary exports."""
    if arcpy.Exists(scratch_gdb_path):
        print(f"Scratch geodatabase already exists: {scratch_gdb_path}")
        return

    scratch_folder = os.path.dirname(scratch_gdb_path)
    scratch_name = os.path.basename(scratch_gdb_path)
    if not os.path.isdir(scratch_folder):
        os.makedirs(scratch_folder, exist_ok=True)

    print(f"Creating scratch geodatabase: {scratch_gdb_path}")
    arcpy.CreateFileGDB_management(scratch_folder, scratch_name)


def export_feature_class(source_fc, scratch_gdb_path, out_name, where_clause=""):
    """Copy a source feature class into the scratch workspace for row-by-row processing."""
    print(f"Exporting source feature class to scratch: {source_fc} -> {out_name}")
    arcpy.FeatureClassToFeatureClass_conversion(source_fc, scratch_gdb_path, out_name, where_clause)


def truncate_dataset(dataset_path):
    """Clear all rows from a target dataset before loading new values."""
    if arcpy.Exists(dataset_path):
        print(f"Truncating: {dataset_path}")
        arcpy.TruncateTable_management(dataset_path)
    else:
        raise ValueError(f"Dataset does not exist and cannot be truncated: {dataset_path}")


def get_domain_map(workspace):
    """Build a lookup table for all coded value domains in the workspace."""
    domain_map = {}
    for domain in arcpy.da.ListDomains(workspace):
        if domain.domainType == "CodedValue":
            coded_values = {str(key): value for key, value in domain.codedValues.items()}
            domain_map[domain.name] = coded_values
    return domain_map


def get_field_domain_name(feature_class, field_name):
    """Return the domain name assigned to a field, or None if the field does not exist."""
    field = arcpy.ListFields(feature_class, field_name)
    if not field:
        return None
    return field[0].domain


def get_coded_value_description(domain_map, domain_name, code):
    """Resolve the coded value description for a domain code.

    If the code or domain does not exist, return an empty string.
    """
    if not domain_name or code is None:
        return ""

    code_str = str(code).strip()
    if code_str == "":
        return ""

    domain_values = domain_map.get(domain_name)
    if not domain_values:
        return ""

    return domain_values.get(code_str, "")


def build_postal_comm_lookup(zipcodes_fc):
    """Build a ZIP-to-community lookup table for postal name resolution."""
    postal_lookup = {}
    if not arcpy.Exists(zipcodes_fc):
        print(f"Zip code feature class not found: {zipcodes_fc}")
        return postal_lookup

    print(f"Building postal community lookup from: {zipcodes_fc}")
    fields = ["ZIP5", "NAME"]
    with arcpy.da.SearchCursor(zipcodes_fc, fields) as cursor:
        for row in cursor:
            zip5 = row[0]
            name = row[1]
            if zip5 is None or name is None:
                continue
            zip5 = str(zip5).strip()
            if zip5 and zip5 not in postal_lookup:
                postal_lookup[zip5] = str(name).strip()

    # Add manual override for the Salt Lake City ZIP code if not present.
    postal_lookup["84114"] = "SALT LAKE CITY"
    return postal_lookup


def map_pt_type(value):
    """Map source point type to the NG911 target place type."""
    if value is None:
        return ""

    normalized = str(value).strip()
    if normalized == "":
        return ""

    return PT_TYPE_MAP.get(normalized, "N/A")


def map_pt_location(value):
    """Map source point location to the NG911 target placement description."""
    if value is None:
        return ""

    normalized = str(value).strip()
    if normalized == "":
        return ""

    return PT_LOCATION_MAP.get(normalized, "N/A")


def map_direction(value):
    """Convert abbreviated compass direction to the full NG911 direction string."""
    if value is None:
        return ""

    normalized = str(value).strip().upper()
    return DIRECTION_MAP.get(normalized, "")


def format_county_name(domain_description):
    """Format a county domain description into the NG911 target county naming convention."""
    if not domain_description:
        return ""

    domain_description = str(domain_description).strip()
    county_match = re.match(r"^[0-9]+\s*-?\s*(.*)$", domain_description)
    if county_match:
        county_text = county_match.group(1).strip()
        if county_text:
            return f"{county_text.upper()} COUNTY"

    return domain_description.upper()


def map_road_class(cartocode):
    """Translate the source road class code into the NG911 road class string."""
    try:
        code = int(cartocode)
    except (TypeError, ValueError):
        return "N/A"

    return ROAD_CLASS_MAP.get(code, "N/A")


def map_one_way(value):
    """Convert the source one-way code into the NG911 one-way symbol."""
    if value is None:
        return ""

    normalized = str(value).strip()
    return ONE_WAY_MAP.get(normalized, "")


def parse_esn(val):
    if isinstance(val, str):
        val = val.strip()
        return int(val) if val.isdigit() else None
    return val


def create_road_alias_rows(alias_insert_cursor, unique_id, objectid, source, updated, effective, expire,
                           rcl_nguid, source_row, domain_map, predir_domain, postdir_domain, posttype_domain,
                           a1_predir, a1_posttype, a1_postdir, a2_predir, a2_posttype, a2_postdir,
                           an_postdir, an_name, a1_name, a2_name):
    """Insert alias rows for a road record into the StreetNameAliasTable."""
    alias_count = 0
    SOURCE_PREDIR_INDEX = 28

    def insert_alias(alias_type, alias_name, alias_postdir, alias_predir, alias_posttype):
        nonlocal alias_count
        if not alias_name:
            return

        alias_count += 1
        a_st_name = alias_name
        a_st_posdir = get_coded_value_description(domain_map, postdir_domain, alias_postdir).upper() if alias_postdir else ""
        a_st_predir = ""
        a_st_posttype = ""

        # AN aliases always use the source PREDIR value.
        if alias_type == "AN":
            a_st_predir = get_coded_value_description(domain_map, predir_domain, source_row[SOURCE_PREDIR_INDEX]).upper()
        else:
            if alias_predir:
                a_st_predir = get_coded_value_description(domain_map, predir_domain, alias_predir).upper()
            else:
                a_st_predir = get_coded_value_description(domain_map, predir_domain, source_row[SOURCE_PREDIR_INDEX]).upper()
            if alias_posttype:
                a_st_posttype = get_coded_value_description(domain_map, posttype_domain, alias_posttype).upper()

        alias_row = [
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
        ]
        alias_insert_cursor.insertRow(alias_row)

    insert_alias("AN", an_name, an_postdir, None, None)
    insert_alias("A1", a1_name, a1_postdir, a1_predir, a1_posttype)
    insert_alias("A2", a2_name, a2_postdir, a2_predir, a2_posttype)

    return alias_count
