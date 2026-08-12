This project creates the Address Point and Road Centerline GIS data for Utah's Next-Generation 911 Database.

The project is written in C# and requires the additional Visual Studio ESRI Extensions:
- ArcGIS Pro SDK for .NET
- ArcGIS Pro SDK for .NET (Utilities)
- ArcGIS Pro SDK for .NET (Migration)
- ArcGIS Pro Metadata Toolkit (this one might be optional)

There are 6 required args for this project:
1. location of output database (ex: C:\temp\ng911_db.gdb)
2. database instance
3. database name
4. database user
5. database password
6. datasets to overwrite or create (ie: roads-t, addresspoints-t) note: the -t indicates to truncate the existing data.

Aug 2026 Update: Pure Python scripts added to remove C# and ArcGIS Desktop dependencies
- Address Points: scripts_arcpy/LoadNG911AddressPoints.py
- Road Centerlines: scripts_arcpy/LoadNG911Roads.py
- Helper script with functions and lookup dictionaries: scripts_arcpy/Helpers.py
