"""
Подготовка географических данных для Plotly-карты.

Читает GeoJSON (EPSG:4326), преобразует координаты в EPSG:32646 (Pulkovo 95 /
WGS84 UTM zone 46), сохраняет результат как JSON-файлы, которые затем
используются Flask-роутом /map для отрисовки интерактивной оффлайн-карты.

Запускать один раз при изменении GeoJSON-файлов:
    python data/prepare_map_data.py
"""

import json
from pathlib import Path

import pyproj

DATA_DIR = Path(__file__).parent
REGION_GEOJSON = DATA_DIR / "russia_regions.geojson"
CITIES_GEOJSON = DATA_DIR / "russia_cities.geojson"

# EPSG:4326 (WGS84 lat/lon) → EPSG:32646 (Pulkovo 95 / WGS84 UTM zone 46N)
TRANSFORMER = pyproj.Transformer.from_crs(
    "EPSG:4326", "EPSG:32646", always_xy=True
)


def transform_coords(coords):
    """Рекурсивно преобразует координаты [lng, lat] -> [x, y] в EPSG:32646."""
    if isinstance(coords, (int, float)):
        return coords
    if isinstance(coords, list):
        if len(coords) == 2 and isinstance(coords[0], (int, float)):
            lng, lat = coords
            x, y = TRANSFORMER.transform(lng, lat)
            return [x, y]
        return [transform_coords(c) for c in coords]
    if isinstance(coords, dict):
        return {k: transform_coords(v) for k, v in coords.items()}
    return coords


def process_file(input_path, output_path, label):
    """Обрабатывает GeoJSON-файл и сохраняет результат."""
    print(f"Читаю {label}: {input_path}")
    with open(input_path) as f:
        data = json.load(f)

    processed = transform_coords(data)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(processed, f, ensure_ascii=False)

    features = processed.get("features", [])
    print(f"  Сохранено {len(features)} объектов")
    return output_path


if __name__ == "__main__":
    print("=" * 60)
    print("Подготовка географических данных для карты")
    print("=" * 60)

    process_file(REGION_GEOJSON, DATA_DIR / "russia_regions_transformed.json", "регионы")
    process_file(CITIES_GEOJSON, DATA_DIR / "russia_cities_transformed.json", "города")

    print("\nГотово!")
    print(f"Файлы сохранены в: {DATA_DIR}")
