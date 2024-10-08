import os
import rasterio
from rasterio.mask import mask
import numpy as np
import geopandas as gpd
from multiprocessing import Pool, cpu_count
import math
from tqdm import tqdm

# Paths to raster files
soil_rasters = {
    'clay': 'data/input/soil/clay.tif',
    'sand': 'data/input/soil/sand.tif',
    'SOC': 'data/input/soil/soc.tif',
    'pH': 'data/input/soil/ph.tif',
    'BD': 'data/input/soil/BD.tif'
}

terrain_rasters = {
    'slope': 'data/output/tif/cog_merged_slope.tif',
    'TPI': 'data/output/tif/cog_merged_tpi.tif',
    'TRI': 'data/output/tif/cog_merged_tri.tif',
    'aspect': 'data/output/tif/cog_merged_aspect.tif'  # Ensure aspect raster is included
}

# Define constants and weights
SOLAR_ALTITUDE = 45  # degrees
SOLAR_AZIMUTH = 180  # degrees (south)
WEIGHTS = {
    'slope': 0.15,
    'TPI': 0.25,
    'TRI': 0.25,
    'SER': 0.35
}
H0 = 1000  # Extraterrestrial solar irradiance in W/m²

# SER Calculation Functions
def adjust_for_impact(value, impact_direction):
    if impact_direction == 'positive':
        return value
    elif impact_direction == 'negative':
        return -value
    else:
        return 0

def calculate_ser_for_geohash(geom):
    ser_value = 0
    try:
        # Process terrain factors
        for factor, raster_path in terrain_rasters.items():
            if factor != 'aspect':  # Exclude aspect for SER calculation
                with rasterio.open(raster_path) as src:
                    out_image, _ = mask(src, [geom], crop=True, all_touched=True)
                    data = out_image[0]
                    valid_data = data[data != src.nodata] if src.nodata is not None else data
                    mean_value = np.mean(valid_data) if valid_data.size > 0 else 0
                    adjusted_value = adjust_for_impact(mean_value, 'positive')
                    ser_value += WEIGHTS.get(factor, 0) * adjusted_value

        # Process soil factors
        soil_values = {}
        for factor, raster in soil_rasters.items():
            with rasterio.open(raster) as src:
                out_image, _ = mask(src, [geom], crop=True, all_touched=True)
                data = out_image[0]
                valid_data = data[data != src.nodata] if src.nodata is not None else data
                mean_value = np.mean(valid_data) if valid_data.size > 0 else 0
                adjusted_value = adjust_for_impact(mean_value, 'negative')
                soil_values[factor] = adjusted_value

        slope_factor = 1 + (ser_value / 90)
        for factor, adjusted_value in soil_values.items():
            ser_value += WEIGHTS.get('slope', 0) * adjusted_value * slope_factor

        return ser_value

    except Exception as e:
        print(f"Error processing SER: {e}")
        return 0

# Solar Potential Calculation Functions
def calculate_angle_of_incidence(slope_deg, aspect_deg):
    slope_rad = math.radians(slope_deg)
    aspect_rad = math.radians(aspect_deg)
    solar_alt_rad = math.radians(SOLAR_ALTITUDE)
    solar_az_rad = math.radians(SOLAR_AZIMUTH)
    
    cos_theta = (math.sin(solar_alt_rad) * math.cos(slope_rad) +
                 math.cos(solar_alt_rad) * math.sin(slope_rad) * math.cos(aspect_rad - solar_az_rad))
    cos_theta = max(min(cos_theta, 1), -1)
    
    theta_deg = math.degrees(math.acos(cos_theta))
    return theta_deg

def calculate_solar_energy_for_geohash(geom):
    try:
        with rasterio.open(terrain_rasters['slope']) as slope_src, rasterio.open(terrain_rasters['aspect']) as aspect_src:
            out_slope, _ = mask(slope_src, [geom], crop=True, all_touched=True)
            out_aspect, _ = mask(aspect_src, [geom], crop=True, all_touched=True)

            slope_data = out_slope[0]
            aspect_data = out_aspect[0]
            valid_mask = (slope_data != slope_src.nodata) & (aspect_data != aspect_src.nodata)

            if not np.any(valid_mask):
                return 0

            slope_mean = np.mean(slope_data[valid_mask])
            aspect_mean = np.mean(aspect_data[valid_mask])
            theta = calculate_angle_of_incidence(slope_mean, aspect_mean)
            solar_potential = H0 * max(math.cos(math.radians(theta)), 0)

            return solar_potential

    except Exception as e:
        print(f"Error processing solar energy: {e}")
        return 0

# Terrain Risk Calculation Functions
def calculate_terrain_risk_for_grid_cell(args):
    geom, ser_value, solar_potential = args
    terrain_risk = 0
    for factor, weight in WEIGHTS.items():
        if factor == 'SER':
            adjusted_ser = adjust_for_impact(ser_value, 'positive')
            terrain_risk += weight * adjusted_ser
            continue

        raster_path = terrain_rasters.get(factor)
        if not os.path.exists(raster_path):
            print(f"Raster file {raster_path} does not exist.")
            continue

        try:
            with rasterio.open(raster_path) as src:
                out_image, _ = mask(src, [geom], crop=True, all_touched=True)
                data = out_image[0]
                valid_data = data[data != src.nodata] if src.nodata is not None else data
                mean_value = np.mean(valid_data) if valid_data.size > 0 else 0
                impact = 'negative' if factor == 'TPI' else 'positive'
                adjusted_value = adjust_for_impact(mean_value, impact)
                terrain_risk += weight * adjusted_value
        except Exception as e:
            print(f"Error processing terrain risk: {e}")
            continue

    return terrain_risk

def process_all_grid_cells(geohash_grid, num_workers):
    # Prepare arguments for multiprocessing
    args_list = [
        (row['geometry'], row['SER'], row['solar'])
        for idx, row in geohash_grid.iterrows()
    ]

    # Process terrain risk in parallel
    with Pool(processes=num_workers) as pool:
        results = list(tqdm(pool.imap(calculate_terrain_risk_for_grid_cell, args_list), total=len(args_list)))

    geohash_grid['Terrain_Risk_Map'] = results
    return geohash_grid

def process_ser_and_solar(geom):
    ser_value = calculate_ser_for_geohash(geom)
    solar_value = calculate_solar_energy_for_geohash(geom)
    return ser_value, solar_value

if __name__ == "__main__":
    geohash_grid_file = 'data/output/gpkg/geohash_resolution_8.gpkg'
    output_file = 'data/output/gpkg/geohash_resolution_8_with_attributes.gpkg'
    num_workers = cpu_count() - 1

    # Load geohash grid
    geohash_grid = gpd.read_file(geohash_grid_file)

    # Extract geometries
    geometries = geohash_grid['geometry'].tolist()

    # Process SER and Solar in parallel
    print("Calculating SER and Solar Potential...")
    with Pool(processes=num_workers) as pool:
        results = list(tqdm(pool.imap(process_ser_and_solar, geometries), total=len(geometries)))

    # Unpack results
    ser_values, solar_values = zip(*results)
    geohash_grid['SER'] = ser_values
    geohash_grid['solar'] = solar_values

    # Process Terrain Risk in parallel
    print("Calculating Terrain Risk...")
    geohash_grid = process_all_grid_cells(geohash_grid, num_workers)

    # Save the updated geohash grid with attributes
    geohash_grid.to_file(output_file, driver='GPKG')
    print(f"Processing complete! File saved as {output_file}")
