"""A tiny synthetic gtfs.zip, shared by the static-GTFS and lookup tests.

Four stops, two lines, three trips. PC15-3 revisits S1 like a real loop line.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

STOPS = """stop_id,stop_name,stop_lat,stop_lon
S1,"P.le Marconi, 20 (Stazione Fs corsia 1)",45.052069,9.705346
S2,"P.le Marconi, 20 (Stazione Fs corsia 2)",45.052135,9.705325
S3,"Ferrovia, corsia 2",45.060000,9.700000
S4,CROCETTA,45.100000,9.800000
"""

ROUTES = """route_id,route_short_name,route_long_name,route_color
PC15,15,Via Roma - Ferrovia,E3001B
PC1,1,"Ferrovia - belvedere - Ferrovia",32CD32
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id
PC15,SVC,PC15-1,Ferrovia,0
PC15,SVC,PC15-2,Marconi,1
PC15,SVC,PC15-3,Loop,1
PC1,SVC,PC1-1,Marconi,0
"""

# Deliberately out of order: PC15-1 lists stop_sequence 2 before 1.
STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
PC15-1,19:40:00,19:40:00,S3,2
PC15-1,19:32:00,19:32:00,S1,1
PC15-2,19:45:00,19:45:00,S1,2
PC15-2,19:32:00,19:32:00,S3,1
PC15-3,21:00:00,21:00:00,S1,1
PC15-3,21:05:00,21:05:00,S3,2
PC15-3,21:12:00,21:12:00,S1,3
PC1-1,06:25:00,06:25:00,S4,1
"""

# 20261029 is added and then removed, so it must not survive parsing.
CALENDAR_DATES = """service_id,date,exception_type
SVC,20261022,1
SVC,20261028,1
OTHER,20261022,1
SVC,20261029,1
SVC,20261029,2
"""

FILES = {
    "stops.txt": STOPS,
    "routes.txt": ROUTES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
    "calendar_dates.txt": CALENDAR_DATES,
}


def write_sample_zip(path: Path) -> Path:
    """Write the sample feed to ``path`` and return it."""
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in FILES.items():
            archive.writestr(name, text)
    return path
