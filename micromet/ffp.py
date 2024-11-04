# Standard library imports
from __future__ import print_function
import warnings
from dataclasses import dataclass
from typing import Optional, Union, Tuple, Dict, List, Any

# Third party imports
import numpy as np
import pandas as pd
import scipy.spatial
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import rasterio
import pyproj
from pyproj import CRS, Transformer
from rasterio.warp import transform_bounds
from affine import Affine



@dataclass
class FootprintInput:
    """Input parameters for footprint calculation"""
    zm: float  # Measurement height above displacement height (z-d) [m]
    z0: Optional[float]  # Roughness length [m]
    umean: Optional[float]  # Mean wind speed at zm [ms-1]
    h: float  # Boundary layer height [m]
    ol: float  # Obukhov length [m]
    sigmav: float  # Standard deviation of lateral velocity fluctuations [ms-1]
    ustar: float  # Friction velocity [ms-1]
    wind_dir: Optional[float]  # Wind direction in degrees

    def validate(self) -> bool:
        """Validate input parameters"""
        if self.zm <= 0:
            raise ValueError("zm must be positive")
        if self.z0 is not None and self.z0 <= 0:
            raise ValueError("z0 must be positive if provided")
        if self.h <= 10:
            raise ValueError("h must be > 10m")
        if self.zm > self.h:
            raise ValueError("zm must be < h")
        if self.sigmav <= 0:
            raise ValueError("sigmav must be positive")
        if self.ustar <= 0.1:
            raise ValueError("ustar must be >= 0.1")
        if self.wind_dir is not None and (self.wind_dir < 0 or self.wind_dir > 360):
            raise ValueError("wind_dir must be between 0 and 360")
        return True


@dataclass
class CoordinateSystem:
    """Represents a coordinate reference system configuration"""
    crs: Union[str, int, CRS]  # EPSG code, proj string, or CRS object
    units: str  # Units of the coordinate system (e.g., 'meters', 'degrees')
    is_geographic: bool  # True if lat/lon, False if projected
    datum: str  # Geodetic datum (e.g., 'WGS84', 'NAD83')

    @classmethod
    def from_epsg(cls, epsg_code: int) -> 'CoordinateSystem':
        """Create CoordinateSystem from EPSG code"""
        crs = CRS.from_epsg(epsg_code)
        return cls(
            crs=crs,
            units=crs.axis_info[0].unit_name,
            is_geographic=crs.is_geographic,
            datum=crs.datum.name if crs.datum else 'Unknown'
        )

    @classmethod
    def from_proj(cls, proj_string: str) -> 'CoordinateSystem':
        """Create CoordinateSystem from proj string"""
        crs = CRS.from_string(proj_string)
        return cls(
            crs=crs,
            units=crs.axis_info[0].unit_name,
            is_geographic=crs.is_geographic,
            datum=crs.datum.name if crs.datum else 'Unknown'
        )

    def get_transform(self, target: 'CoordinateSystem') -> Transformer:
        """Get transformer to convert to target coordinate system"""
        return Transformer.from_crs(self.crs, target.crs, always_xy=True)


@dataclass
class FootprintConfig:
    """Enhanced configuration with coordinate system support"""
    origin_distance: float
    measurement_height: float
    roughness_length: float
    domain_size: Tuple[float, float, float, float]
    grid_resolution: float
    station_coords: Tuple[float, float]
    coordinate_system: CoordinateSystem
    working_crs: Optional[CoordinateSystem] = None  # CRS for internal calculations


class CoordinateTransformer:
    """Handle coordinate transformations between different systems"""

    def __init__(self,
                 source_crs: CoordinateSystem,
                 target_crs: Optional[CoordinateSystem] = None):
        self.source_crs = source_crs
        self.target_crs = target_crs
        self.transformer = None

        if target_crs:
            self.transformer = source_crs.get_transform(target_crs)

    def transform_coords(self,
                         x: np.ndarray,
                         y: np.ndarray,
                         direction: str = 'forward') -> Tuple[np.ndarray, np.ndarray]:
        """Transform coordinates between source and target CRS"""
        if self.transformer is None:
            return x, y

        if direction == 'forward':
            return self.transformer.transform(x, y)
        elif direction == 'inverse':
            return self.transformer.transform(x, y, direction='INVERSE')
        else:
            raise ValueError("direction must be 'forward' or 'inverse'")

    def transform_bounds(self,
                         bounds: Tuple[float, float, float, float],
                         direction: str = 'forward') -> Tuple[float, float, float, float]:
        """Transform bounding box coordinates"""
        if self.transformer is None:
            return bounds

        xmin, ymin, xmax, ymax = bounds
        if direction == 'forward':
            x_trans, y_trans = self.transformer.transform([xmin, xmax], [ymin, ymax])
        else:
            x_trans, y_trans = self.transformer.transform([xmin, xmax], [ymin, ymax],
                                                          direction='INVERSE')

        return (min(x_trans), min(y_trans), max(x_trans), max(y_trans))


class FootprintCalculator:
    """Handles footprint calculations using refactored FFP model"""

    def __init__(self):
        # Model parameters
        self.a = 1.4524
        self.b = -1.9914
        self.c = 1.4622
        self.d = 0.1359
        self.ac = 2.17
        self.bc = 1.66
        self.cc = 20.0
        self.oln = 5000
        self.k = 0.4

    def calculate_footprint(self,
                            inputs: FootprintInput,
                            domain: Optional[Tuple[float, float, float, float]] = None,
                            nx: int = 1000,
                            smooth_data: bool = True) -> Dict[str, np.ndarray]:
        """Calculate single footprint for given input parameters"""

        inputs.validate()

        # Setup domain if not provided
        if domain is None:
            domain = [-1000., 1000., -1000., 1000.]
        xmin, xmax, ymin, ymax = domain

        # Create grid
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, nx)
        x_2d, y_2d = np.meshgrid(x, y)

        # Calculate footprint
        f_2d = self._calc_footprint_matrix(inputs, x_2d, y_2d)

        if smooth_data:
            f_2d = self._smooth_footprint(f_2d)

        return {
            'x_2d': x_2d,
            'y_2d': y_2d,
            'f_2d': f_2d
        }

    def calculate_footprint_climatology(self,
                                        input_series: List[FootprintInput],
                                        domain: Optional[Tuple[float, float, float, float]] = None,
                                        nx: int = 1000,
                                        smooth_data: bool = True) -> Dict[str, Any]:
        """Calculate footprint climatology from series of inputs"""

        # Setup domain if not provided
        if domain is None:
            domain = [-1000., 1000., -1000., 1000.]

        xmin, xmax, ymin, ymax = domain

        # Create grid
        x = np.linspace(xmin, xmax, nx)
        y = np.linspace(ymin, ymax, nx)
        x_2d, y_2d = np.meshgrid(x, y)

        # Initialize climatology grid
        fclim_2d = np.zeros(x_2d.shape)
        valid_count = 0

        # Process each input
        for inputs in input_series:
            try:
                inputs.validate()
                f_2d = self._calc_footprint_matrix(inputs, x_2d, y_2d)
                if f_2d is not None:
                    fclim_2d += f_2d
                    valid_count += 1
            except Exception as e:
                warnings.warn(f"Error processing input: {str(e)}")
                continue

        if valid_count == 0:
            raise ValueError("No valid footprints calculated")

        # Normalize and smooth
        fclim_2d /= valid_count
        if smooth_data:
            fclim_2d = self._smooth_footprint(fclim_2d)

        return {
            'x_2d': x_2d,
            'y_2d': y_2d,
            'fclim_2d': fclim_2d,
            'n': valid_count
        }

    def _calc_footprint_matrix(self,
                               inputs: FootprintInput,
                               x_2d: np.ndarray,
                               y_2d: np.ndarray) -> np.ndarray:
        """Calculate footprint matrix for given inputs and grid"""
        # Convert to polar coordinates
        rho = np.sqrt(x_2d ** 2 + y_2d ** 2)
        theta = np.arctan2(x_2d, y_2d)

        # Rotate coordinates if wind direction provided
        if inputs.wind_dir is not None:
            theta = theta - inputs.wind_dir * np.pi / 180.

        # Calculate scaled parameters
        if inputs.z0 is not None:
            xstar = self._calc_xstar_z0(inputs, rho, theta)
        else:
            xstar = self._calc_xstar_umean(inputs, rho, theta)

        # Calculate footprint
        f_2d = np.zeros(x_2d.shape)
        valid = xstar > self.d

        f_2d[valid] = self._calc_footprint_values(inputs, xstar[valid], rho[valid], theta[valid])

        return f_2d

    def _smooth_footprint(self, f_2d: np.ndarray) -> np.ndarray:
        """Apply smoothing to footprint"""
        kernel = np.matrix('0.05 0.1 0.05; 0.1 0.4 0.1; 0.05 0.1 0.05')
        f_2d = scipy.signal.convolve2d(f_2d, kernel, mode='same')
        f_2d = scipy.signal.convolve2d(f_2d, kernel, mode='same')
        return f_2d

    def _calc_xstar_z0(self, inputs: FootprintInput, rho: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """
        Calculate xstar using roughness length (z0) approach.

        Args:
            inputs: FootprintInput object containing model parameters
            rho: Array of distances from measurement point
            theta: Array of angles from measurement point

        Returns:
            Array of scaled x coordinates (xstar)

        References:
            Kljun et al. (2015) A simple two-dimensional parameterisation for Flux Footprint Prediction (FFP)
        """
        # Calculate psi_m stability correction
        if inputs.ol <= 0 or inputs.ol >= self.oln:  # Unstable or very stable
            xx = (1 - 19.0 * inputs.zm / inputs.ol) ** 0.25
            psi_m = (np.log((1 + xx ** 2) / 2.) +
                     2. * np.log((1 + xx) / 2.) -
                     2. * np.arctan(xx) + np.pi / 2)
        else:  # Stable
            psi_m = -5.3 * inputs.zm / inputs.ol

        # Calculate xstar including stability correction
        xstar = (rho * np.cos(theta) / inputs.zm *
                 (1. - (inputs.zm / inputs.h)) /
                 (np.log(inputs.zm / inputs.z0) - psi_m))

        return xstar

    def _calc_xstar_umean(self, inputs: FootprintInput, rho: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """
        Calculate xstar using mean wind speed (umean) approach when z0 is not available.

        Args:
            inputs: FootprintInput object containing model parameters
            rho: Array of distances from measurement point
            theta: Array of angles from measurement point

        Returns:
            Array of scaled x coordinates (xstar)
        """
        # Calculate xstar using mean wind speed
        xstar = (rho * np.cos(theta) / inputs.zm *
                 (1. - (inputs.zm / inputs.h)) /
                 (inputs.umean / inputs.ustar * self.k))

        return xstar

    def _calc_footprint_values(self, inputs: FootprintInput, xstar: np.ndarray,
                               rho: np.ndarray, theta: np.ndarray) -> np.ndarray:
        """
        Calculate footprint function values for given scaled coordinates.

        Args:
            inputs: FootprintInput object containing model parameters
            xstar: Array of scaled x coordinates
            rho: Array of distances from measurement point
            theta: Array of angles from measurement point

        Returns:
            Array of footprint function values
        """
        # Initialize output array
        f_2d = np.zeros_like(xstar)

        # Only calculate for valid xstar values (where xstar > d)
        valid = xstar > self.d

        if not np.any(valid):
            return f_2d

        # Calculate scaled crosswind-integrated footprint for valid points
        fstar_ci = np.zeros_like(xstar)
        fstar_ci[valid] = (self.a *
                           (xstar[valid] - self.d) ** self.b *
                           np.exp(-self.c / (xstar[valid] - self.d)))

        # Calculate real-scale crosswind-integrated footprint
        f_ci = np.zeros_like(xstar)

        if inputs.z0 is not None:
            # Use z0 approach
            if inputs.ol <= 0 or inputs.ol >= self.oln:
                xx = (1 - 19.0 * inputs.zm / inputs.ol) ** 0.25
                psi_f = (np.log((1 + xx ** 2) / 2.) +
                         2. * np.log((1 + xx) / 2.) -
                         2. * np.arctan(xx) + np.pi / 2)
            else:
                psi_f = -5.3 * inputs.zm / inputs.ol

            denom = np.log(inputs.zm / inputs.z0) - psi_f
            if denom > 0:  # Avoid division by zero or negative values
                f_ci[valid] = (fstar_ci[valid] / inputs.zm *
                               (1. - (inputs.zm / inputs.h)) / denom)
        else:
            # Use umean approach
            f_ci[valid] = (fstar_ci[valid] / inputs.zm *
                           (1. - (inputs.zm / inputs.h)) /
                           (inputs.umean / inputs.ustar * self.k))

        # Calculate sigY* - scaled crosswind dispersion
        sigystar = np.zeros_like(xstar)
        sigystar[valid] = (self.ac *
                           np.sqrt(self.bc * xstar[valid] ** 2 /
                                   (1 + self.cc * xstar[valid])))

        # Calculate real-scale sigY
        ol_calc = -1E6 if abs(inputs.ol) > self.oln else inputs.ol

        if ol_calc <= 0:  # Unstable
            scale_const = min(1.0, 1E-5 * abs(inputs.zm / ol_calc) ** (-1) + 0.80)
        else:  # Stable
            scale_const = min(1.0, 1E-5 * abs(inputs.zm / ol_calc) ** (-1) + 0.55)

        sigy = np.zeros_like(xstar)
        sigy[valid] = (sigystar[valid] / scale_const *
                       inputs.zm *
                       inputs.sigmav / inputs.ustar)

        # Calculate 2D footprint where sigy is non-zero
        valid_sigy = valid & (sigy > 0)
        if np.any(valid_sigy):
            f_2d[valid_sigy] = (f_ci[valid_sigy] /
                                (np.sqrt(2 * np.pi) * sigy[valid_sigy]) *
                                np.exp(-(rho[valid_sigy] * np.sin(theta[valid_sigy])) ** 2 /
                                       (2. * sigy[valid_sigy] ** 2)))

        return f_2d


class FootprintProcessor:
    """Process and handle georeferenced flux footprint data"""

    def __init__(self, config: FootprintConfig):
        self.config = config
        self.calculator = FootprintCalculator()
        self.transformer = self._setup_projection()

    def _setup_projection(self) -> pyproj.Transformer:
        """Initialize coordinate transformer"""
        return pyproj.Transformer.from_crs(
            self.config.projection,
            self.config.projection,
            always_xy=True
        )

    def process_time_data(self, timestamp: str) -> pd.Timestamp:
        """Process timestamp into pandas Timestamp"""
        if isinstance(timestamp, str):
            if '2400' in timestamp:
                timestamp = timestamp.replace('2400', '0000')
                dt_obj = pd.Timestamp(timestamp) + pd.Timedelta(days=1)
                return dt_obj
            return pd.Timestamp(timestamp)
        return timestamp

    def calculate_georeferenced_footprint(self,
                                          inputs: FootprintInput) -> Dict[str, np.ndarray]:
        """Calculate footprint and transform to geographic coordinates"""

        # Calculate footprint
        result = self.calculator.calculate_footprint(
            inputs,
            domain=self.config.domain_size,
            nx=int((self.config.domain_size[1] - self.config.domain_size[0])
                   / self.config.grid_resolution)
        )

        # Transform coordinates
        x_geo = result['x_2d'] + self.config.station_coords[0]
        y_geo = result['y_2d'] + self.config.station_coords[1]

        if self.config.projection != 'native':
            x_geo, y_geo = self.transformer.transform(x_geo, y_geo)

        return {
            'x_2d': x_geo,
            'y_2d': y_geo,
            'f_2d': result['f_2d']
        }


class FootprintPlotter:
    """Handle plotting of flux footprints"""

    def __init__(self, processor: FootprintProcessor):
        self.processor = processor
        self.default_figsize = (10, 10)

    def plot_footprint(self,
                       x_coords: np.ndarray,
                       y_coords: np.ndarray,
                       footprint: np.ndarray,
                       timestamp: Union[str, pd.Timestamp],
                       save_path: Optional[str] = None,
                       **kwargs) -> Tuple[plt.Figure, plt.Axes]:
        """Plot footprint with optional saving"""
        timestamp = self.processor.process_time_data(timestamp)

        # Create figure
        fig, ax = plt.subplots(figsize=kwargs.get('figsize', self.default_figsize))

        # Plot footprint
        mesh = ax.pcolormesh(x_coords, y_coords, footprint,
                             norm=kwargs.get('norm', LogNorm()))

        # Add colorbar
        cbar = fig.colorbar(mesh)
        cbar.set_label(label='Footprint Contribution',
                       fontsize='large',
                       rotation=270,
                       labelpad=15)

        # Customize plot
        station_x, station_y = self.processor.config.station_coords
        dist = self.processor.config.origin_distance

        ax.grid(linestyle='--', alpha=0.5)
        ax.set_xlim(station_x - dist, station_x + dist)
        ax.set_ylim(station_y - dist, station_y + dist)
        ax.set_title(f'Flux Footprint - {timestamp}')

        if save_path:
            fig.savefig(save_path, dpi=300, bbox_inches='tight')

        return fig, ax

    def plot_footprint_on_raster(self,
                                 x_coords: np.ndarray,
                                 y_coords: np.ndarray,
                                 footprint: np.ndarray,
                                 raster_path: str,
                                 timestamp: Union[str, pd.Timestamp],
                                 save_path: Optional[str] = None,
                                 **kwargs) -> Tuple[plt.Figure, plt.Axes]:
        """Plot footprint overlaid on raster background"""
        # Load raster
        with rasterio.open(raster_path) as src:
            raster_data = src.read(1)
            raster_transform = src.transform

        timestamp = self.processor.process_time_data(timestamp)

        # Create figure
        fig, ax = plt.subplots(figsize=kwargs.get('figsize', self.default_figsize))

        # Plot raster
        ax.imshow(raster_data, extent=rasterio.plot.plotting_extent(src),
                  cmap=kwargs.get('raster_cmap', 'viridis'),
                  alpha=kwargs.get('raster_alpha', 0.5))

        # Plot footprint
        mesh = ax.pcolormesh(x_coords, y_coords, footprint,
                             norm=kwargs.get('norm', LogNorm()),
                             alpha=kwargs.get('footprint_alpha', 0.7))

        # Add colorbar
        cbar = fig.colorbar(mesh)
        cbar.set_label(label='Footprint Contribution',
                       fontsize='large',
                       rotation=270,
                       labelpad=15)

        # Customize plot
        station_x, station_y = self.processor.config.station_coords
        ax.plot(station_x, station_y, 'r^', markersize=10, label='Station')

        ax.grid(linestyle='--', alpha=0.3)
        ax.set_title(f'Flux Footprint - {timestamp}')
        ax.legend()

        if save_path:
            fig.savefig(save_path, dpi=300, bbox_inches='tight')

        return fig, ax

class RasterProcessor:
    """Handle raster data processing for footprint analysis"""

    def __init__(self, processor: FootprintProcessor):
        self.processor = processor

    def weight_raster_values(self,
                             x_coords: np.ndarray,
                             y_coords: np.ndarray,
                             footprint: np.ndarray,
                             raster_data: np.ndarray,
                             raster_transform: Affine) -> float:
        """Weight raster values by footprint contribution"""
        # Create KD-tree for efficient nearest neighbor search
        raster_coords = rasterio.transform.xy(
            raster_transform,
            np.arange(raster_data.shape[0]),
            np.arange(raster_data.shape[1])
        )
        points = np.column_stack([coord.flatten() for coord in raster_coords])
        tree = scipy.spatial.cKDTree(points)

        # Prepare footprint data
        valid_points = ~np.isnan(footprint)
        footprint_points = np.column_stack([
            x_coords[valid_points],
            y_coords[valid_points]
        ])
        footprint_values = footprint[valid_points]

        # Find nearest raster points
        distances, indices = tree.query(footprint_points)

        # Weight raster values
        raster_values = raster_data.flatten()[indices]
        weighted_sum = np.sum(raster_values * footprint_values)
        total_weight = np.sum(footprint_values)

        return weighted_sum / total_weight if total_weight > 0 else 0



class EnhancedFootprintProcessor:
    """Enhanced processor with coordinate system support"""

    def __init__(self, config: FootprintConfig):
        self.config = config
        self.calculator = FootprintCalculator()

        # Setup working CRS if not specified
        if self.config.working_crs is None:
            if self.config.coordinate_system.is_geographic:
                # Use UTM zone based on station coordinates
                utm_zone = self._get_utm_zone(*self.config.station_coords)
                self.config.working_crs = CoordinateSystem.from_epsg(utm_zone)
            else:
                self.config.working_crs = self.config.coordinate_system

        # Setup transformers
        self.to_working = CoordinateTransformer(
            self.config.coordinate_system,
            self.config.working_crs
        )
        self.from_working = CoordinateTransformer(
            self.config.working_crs,
            self.config.coordinate_system
        )

    def _get_utm_zone(self, lon: float, lat: float) -> int:
        """Calculate UTM zone from lat/lon"""
        zone_number = int((lon + 180) / 6) + 1

        if lat >= 0:
            # Northern hemisphere
            return 32600 + zone_number
        else:
            # Southern hemisphere
            return 32700 + zone_number

    def transform_to_working(self, x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Transform coordinates to working CRS"""
        return self.to_working.transform_coords(x, y)

    def transform_from_working(self, x: np.ndarray, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Transform coordinates from working CRS"""
        return self.from_working.transform_coords(x, y)

    def calculate_georeferenced_footprint(self, inputs: FootprintInput) -> Dict[str, np.ndarray]:
        """Calculate footprint with coordinate transformations"""
        # Transform domain bounds to working CRS
        working_bounds = self.to_working.transform_bounds(self.config.domain_size)

        # Transform station coordinates
        station_x, station_y = self.transform_to_working(
            np.array([self.config.station_coords[0]]),
            np.array([self.config.station_coords[1]])
        )

        # Calculate footprint in working CRS
        result = self.calculator.calculate_footprint(
            inputs,
            domain=working_bounds,
            nx=int((working_bounds[2] - working_bounds[0]) / self.config.grid_resolution)
        )

        # Transform results back to original CRS
        x_orig, y_orig = self.transform_from_working(result['x_2d'], result['y_2d'])

        return {
            'x_2d': x_orig,
            'y_2d': y_orig,
            'f_2d': result['f_2d']
        }


class RasterCoordinateProcessor:
    """Handle raster coordinate transformations"""

    def __init__(self, processor: EnhancedFootprintProcessor):
        self.processor = processor

    def transform_raster(self,
                         raster_path: str,
                         target_crs: CoordinateSystem) -> Tuple[np.ndarray, Affine]:
        """Transform raster to target coordinate system"""
        with rasterio.open(raster_path) as src:
            # Create transformer
            transformer = Transformer.from_crs(
                src.crs,
                target_crs.crs,
                always_xy=True
            )

            # Transform bounds
            bounds = transform_bounds(
                src.crs,
                target_crs.crs,
                *src.bounds,
                transformer=transformer
            )

            # Read and reproject data
            data = src.read(1)

            # Calculate new transform
            width_ratio = (bounds[2] - bounds[0]) / (src.bounds[2] - src.bounds[0])
            height_ratio = (bounds[3] - bounds[1]) / (src.bounds[3] - src.bounds[1])

            new_transform = Affine(
                src.transform.a * width_ratio,
                src.transform.b,
                bounds[0],
                src.transform.d,
                src.transform.e * height_ratio,
                bounds[1]
            )

            return data, new_transform


def create_processor_with_crs(config_dict: Dict) -> EnhancedFootprintProcessor:
    """Create processor with coordinate system configuration"""
    # Extract CRS information
    crs_info = config_dict.pop('coordinate_system')
    if isinstance(crs_info, (str, int)):
        if isinstance(crs_info, str) and crs_info.startswith('EPSG:'):
            coord_sys = CoordinateSystem.from_epsg(int(crs_info[5:]))
        elif isinstance(crs_info, int):
            coord_sys = CoordinateSystem.from_epsg(crs_info)
        else:
            coord_sys = CoordinateSystem.from_proj(crs_info)
    else:
        coord_sys = CoordinateSystem(**crs_info)

    # Create config with coordinate system
    config = FootprintConfig(
        coordinate_system=coord_sys,
        **config_dict
    )

    return EnhancedFootprintProcessor(config)


def create_processor(config_dict: Dict) -> FootprintProcessor:
    """Create FootprintProcessor from configuration dictionary"""
    config = FootprintConfig(**config_dict)
    return FootprintProcessor(config)
