# Bundled geographic data

| File | Source | Licence |
|---|---|---|
| `us_zips.csv.gz` | US Census Bureau ZCTA gazetteer | Public domain (US Government work) |
| `us_places.csv.gz` | US Census Bureau places gazetteer | Public domain (US Government work) |
| `cl_areas.json` | craigslist region list (host, coordinates, country) | Compiled from craigslist's own public region index |
| `world_places.csv.gz` | [GeoNames](https://www.geonames.org/) `cities1000` | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

`world_places.csv.gz` is derived from the GeoNames `cities1000` dataset: US entries are removed
(the Census gazetteer above already covers them in more detail), only the primary and ASCII names
are kept, and at most the four most-populous places sharing a name are retained. Columns are
`name,country,lat,lon`.

**Attribution:** this work includes data from GeoNames, licensed under CC BY 4.0.
