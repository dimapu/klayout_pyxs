# coding: utf-8
"""klayout_pyxs.py

"""

#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#

# A feasibility study for a cross section generation using
# boolean operations. See "cmos.pyxs" for a brief description of the
# commands available and some examples.

# TODO: use a much smaller dbu for the simulation to have a really small delta
# the paths used for generating the masks are somewhat too thick
# TODO: the left and right areas are not treated correctly

from importlib import reload
import math
import os
import re

import klayout_pyxs

try:
    reload(klayout_pyxs)
    reload(klayout_pyxs.utils)
    reload(klayout_pyxs.geometry_2d)
except:
    pass

from klayout_pyxs import HAS_PYA

from klayout_pyxs import Box
from klayout_pyxs import Edge
from klayout_pyxs import Point
from klayout_pyxs import Polygon

if HAS_PYA:
    # Imports for KLayout plugin
    from klayout_pyxs import Application
    from klayout_pyxs import MessageBox
    from klayout_pyxs import Action
    from klayout_pyxs import FileDialog

from klayout_pyxs.utils import print_info, int_floor, make_iterable, info
from klayout_pyxs.geometry_2d import ep, EP, LayoutData, MaterialData, MaskData
from klayout_pyxs.layer_parameters import string_to_layer_info

info('Module klayout_pyxs.pyxs_lib.py reloaded')


class XSectionGenerator(object):
    """ The main class that creates a cross-section file
    """
    def __init__(self, file_path):
        """
        Parameters
        ----------
        file_path : str
        """
        # TODO: adjust this path:
        self._file_path = file_path
        self._lyp_file = None
        self._ep = ep
        self._flipped = False
        self._air, self._air_below = None, None
        self._delta = None
        self._extend = None
        self._below = None
        self._depth = None
        self._height = None

    def layer(self, layer_spec):
        """ Fetches an input layer from the original layout.

        Parameters
        ----------
        layer_spec : str

        Returns
        -------
        ld : LayerData

        """
        ld = LayoutData([], self)  # empty
        # collect shapes from the corresponding layer into ld._polygons
        ld.load(self._layout, self._cell,
                self._line_dbu.bbox().enlarge(
                    Point(self._extend, self._extend)),
                layer_spec)
        return ld

    @print_info(False)
    def mask(self, layer_data):
        """ Designates the layout_data object as a litho pattern (mask).

        This is the starting point for structured grow or etch operations.

        Parameters
        ----------
        layer_data : LayoutData

        Returns
        -------
        res : klayout_pyxs_repo.klayout_pyxs.geometry_2d.MaterialData
        """
        crossing_points = []

        info('    layer_data: {}'.format(layer_data))

        info('    n polygons in layer_data: {}'.format(layer_data.n_poly))

        for polygon in layer_data.data:
            info('    polygon: {}'.format(polygon))
            for edge_dbu in polygon.each_edge():
                info('        edge: {}'.format(edge_dbu))
                if self._line_dbu.crossed_by(edge_dbu):
                    info('        crosses!')

                if (self._line_dbu.crossed_by(edge_dbu) and
                        (self._line_dbu.side_of(edge_dbu.p1) > 0 or
                         self._line_dbu.side_of(edge_dbu.p2) > 0)):
                    info('        inside if')
                    # compute the crossing point of "edge" and "line" in
                    # database units
                    # confine the point to the length of the line
                    z = (float(edge_dbu.dx()) * (edge_dbu.p1.y -
                                                 self._line_dbu.p1.y) -
                         float(edge_dbu.dy()) * (edge_dbu.p1.x -
                                                 self._line_dbu.p1.x)) / \
                        (float(edge_dbu.dx()) * (self._line_dbu.p2.y -
                                                 self._line_dbu.p1.y) -
                         float(edge_dbu.dy()) * (self._line_dbu.p2.x -
                                                 self._line_dbu.p1.x))
                    z = math.floor(z * self._line_dbu.length() + 0.5)
                    if z < -self._extend:
                        z = -self._extend
                    elif z > self._line_dbu.length() + self._extend:
                        z = self._line_dbu.length() + self._extend

                    v = (edge_dbu.dy() * self._line_dbu.dx() -
                         edge_dbu.dx() * self._line_dbu.dy())
                    if v < 0:
                        s = -1
                    elif v == 0:
                        s = 0
                    else:
                        s = 1

                    # store that along with the orientation of the edge
                    # (+1: "enter geometry", -1: "leave geometry")
                    info('        appending x-point [{}, {}]'.format(z, s))
                    crossing_points.append([z, s])

        # compress the crossing points by collecting all of those which
        # cut the measure line at the same position
        compressed_crossing_points = []
        last_z = None
        sum_s = 0
        crossing_points.sort()
        for z, s in crossing_points:
            if z == last_z:
                sum_s += s
            else:
                if sum_s != 0:
                    compressed_crossing_points.append([last_z, sum_s])
                last_z, sum_s = z, s

        if last_z and sum_s != 0:
            compressed_crossing_points.append([last_z, sum_s])

        # create the final intervals by selecting those crossing points which
        # denote an entry or leave point into or out of drawn geometry. This
        # basically does a merge of all drawn shapes.
        return self._xpoints_to_mask(compressed_crossing_points)

    # @property
    def air(self):
        return self._air

    # @property
    def bulk(self):
        """ Return a material describing the wafer body

        Return
        ------
        bulk : klayout_pyxs_repo.klayout_pyxs.geometry_2d.MaterialData
        """

        return MaterialData(self._bulk.data, self)

    def output(self, layer_spec=None, layer_data=None, output_layers=None,
               *args):
        """Outputs a material object to the output layout

        Can be used for a single material (layer_spec and layer_data pair),
        or for a list of materials passed through an output_layers
        dictionary.

        Parameters
        ----------
        layer_spec : str
            layer specification
        layer_data : LayoutData

        output_layers :  Dict[str, LayoutData]
            keys have the same meaning as layer_spec, values are layer_data.
        """
        if (layer_spec is None or layer_data is None) and output_layers is None:
            raise ValueError('layer_spec and layer_data cannot be None when no'
                             'output_layers is given.')

        if output_layers:
            if layer_spec or layer_data:
                raise ValueError('Cannot use both layer_spec/layer_data'
                                 'and output_layers. Use only one of them.')
            for ls, ld in output_layers.items():
                self.output(layer_spec=ls, layer_data=ld)
            return

        # process layer_spec / layer_data pair
        if not isinstance(layer_data, LayoutData):
            raise TypeError("'output' method: second parameter must be "
                            "a geometry object. {} is given"
                            .format(type(layer_data)))

        ls = string_to_layer_info(layer_spec)
        li = self._target_layout.insert_layer(ls)
        shapes = self._target_layout.cell(self._target_cell).shapes(li)

        # confine the shapes to the region of interest
        for polygon in self._ep.boolean_to_polygon(
                [Polygon(self._roi)], layer_data.data,
                EP.ModeAnd, True, True):
            shapes.insert(polygon)

    def output_raw(self, layer_spec, d):
        """ For debugging only
        """
        ls = string_to_layer_info(layer_spec)
        li = self._target_layout.insert_layer(ls)
        shapes = self._target_layout.cell(self._target_cell).shapes(li)
        shapes.insert(d)
        return shapes

    @print_info(False)
    def all(self):
        """ A pseudo-mask, covering the whole wafer

        Return
        ------
        res : MaterialData
        """
        e = self._extend
        info('e = {}'.format(e))

        line_dbu = self._line_dbu
        info('line_dbu = {}'.format(line_dbu))

        res = self._xpoints_to_mask([[-e, 1], [line_dbu.length() + e, -1]])

        info('    all().res = {}'.format(res))
        return res

    def flip(self):
        """ Start or end backside processing

        """
        self._air, self._air_below = self._air_below, self._air
        self._flipped = not self._flipped

    def diffuse(self, *args, **kwargs):
        """ Same as deposit()
        """
        return self.all().grow(*args, **kwargs)

    def deposit(self, *args, **kwargs):
        """ Deposits material as a uniform sheet.

        Equivalent to all.grow(...)

        Return
        ------
        res : MaterialData
        """
        return self.all().grow(*args, **kwargs)

    @print_info(False)
    def grow(self, *args, **kwargs):
        """ Same as deposit()
        """
        all = self.all()
        info(all)
        return all.grow(*args, **kwargs)

    def etch(self, *args, **kwargs):
        """ Uniform etching

        Equivalent to all.etch(...)

        """
        return self.all().etch(*args, **kwargs)

    @print_info(False)
    def planarize(self, *args, **kwargs):
        """ Planarization
        """
        downto = None
        less = None
        to = None
        into = []

        for k, v in kwargs.items():
            if k == 'downto':
                downto = make_iterable(v)
                for i in downto:
                    if not isinstance(i, MaterialData):
                        raise TypeError("'planarize' method: 'downto' expects "
                                        "a material parameter or an array "
                                        "of such")

            elif k == 'into':
                into = make_iterable(v)
                for i in into:
                    if not isinstance(i, MaterialData):
                        raise TypeError("'planarize' method: 'into' expects "
                                        "a material parameter or an array "
                                        "of such")
            elif k == 'less':
                less = int_floor(0.5 + float(v) / self.dbu)
            elif k == 'to':
                to = int_floor(0.5 + float(v) / self.dbu)

        if not into:
            raise ValueError("'planarize' requires an 'into' argument")

        info('   downto = {}'.format(downto))
        info('   less = {}'.format(less))
        info('   to = {}'.format(to))
        info('   into = {}'.format(into))

        if downto:
            downto_data = None
            if len(downto) == 1:
                downto_data = downto[0].data
            else:
                for i in downto:
                    if len(downto_data) == 0:
                        downto_data = i.data
                    else:
                        downto_data = self._ep.boolean_p2p(
                                i.data, downto_data,
                                EP.ModeOr)

            # determine upper bound of material
            if downto_data:
                for p in downto_data:
                    yt = p.bbox().top
                    yb = p.bbox().bottom
                    to = to or yt
                    if not self._flipped:
                        to = max([to, yt, yb])
                    else:
                        to = min([to, yt, yb])
                info('    to = {}'.format(to))

        elif into and not to:

            # determine upper bound of our material
            for i in into:
                for p in i.data:
                    yt = p.bbox().top
                    yb = p.bbox().bottom
                    to = to or yt
                    if not self._flipped:
                        to = max([to, yt, yb])
                    else:
                        to = min([to, yt, yb])

        if to is not None:
            info('    to is true')
            less = less or 0
            if self._flipped:
                removed_box = Box(-self._extend,
                                  -self.depth_dbu - self.below_dbu,
                                  self._line_dbu.length() + self._extend,
                                  to + less)
            else:
                removed_box = Box(-self._extend,
                                  to - less,
                                  self._line_dbu.length() + self._extend,
                                  self.height_dbu)

            rem = LayoutData([], self)
            for i in into:
                rem.add(i.and_([Polygon(removed_box)]))
                i.sub([Polygon(removed_box)])

            self.air().add(rem)

            # self.air().close_gaps()

    def set_thickness_scale_factor(self, factor):
        """Configures layer thickness scale factor

        to have better proportions
        """
        self._thickness_scale_factor = factor

    def set_output_parameters(self, filename=None, format=None):
        print('set_output_parameters() has no effect in pyxs.')

    @print_info(False)
    def set_delta(self, x):
        """Configures the accuracy parameter
        """
        self._delta = int_floor(x / self._dbu + 0.5)
        info('XSG._delta set to {}'.format(self._delta))

    def delta(self, x):
        self._delta = int_floor(x / self._dbu + 0.5)
        info('XSG._delta set to {}'.format(self._delta))

    @property
    def delta_dbu(self):
        return self._delta

    @print_info(False)
    def set_height(self, x):
        """ Configures the height of the processing window

        """
        self._height = int_floor(x / self._dbu + 0.5)
        info('XSG._height set to {}'.format(self._height))
        self._update_basic_regions()

    @print_info(False)
    def height(self, x):
        """ Configures the height of the processing window

        """
        self._height = int_floor(x / self._dbu + 0.5)
        info('XSG._height set to {}'.format(self._height))
        self._update_basic_regions()

    @property
    def height_dbu(self):
        return self._height

    @print_info(False)
    def depth(self, x):
        """ Configures the depth of the processing window
        or the wafer thickness for backside processing (see below)

        """
        self._depth = int_floor(x / self._dbu + 0.5)
        info('XSG._depth set to {}'.format(self._depth))
        self._update_basic_regions()

    @print_info(False)
    def set_depth(self, x):
        """ Configures the depth of the processing window
        or the wafer thickness for backside processing (see below)

        """
        self._depth = int_floor(x / self._dbu + 0.5)
        info('XSG._depth set to {}'.format(self._depth))
        self._update_basic_regions()

    @property
    def depth_dbu(self):
        return self._depth

    @print_info(False)
    def below(self, x):
        """ Configures the lower height of the processing window for backside processing

        Parameters
        ----------
        x : float
            depth below the wafer in um,

        """
        self._below = int_floor(x / self._dbu + 0.5)
        info('XSG._below set to {}'.format(self._below))
        self._update_basic_regions()

    @print_info(False)
    def set_below(self, x):
        """ Configures the lower height of the processing window for backside processing

        Parameters
        ----------
        x : float
            depth below the wafer in um,

        """
        self._below = int_floor(x / self._dbu + 0.5)
        info('XSG._below set to {}'.format(self._below))
        self._update_basic_regions()

    @property
    def below_dbu(self):
        return self._below

    def set_extend(self, x):
        """ Configures the computation margin

        """
        self._extend = int_floor(x / self._dbu + 0.5)
        self._update_basic_regions()

    @property
    def extend_dbu(self):
        return self._extend

    @property
    def width_dbu(self):
        """ Cross-section width.

        Determined by the ruler width.
        """
        return self._line_dbu.length()

    def background(self):
        """
        Returns
        -------
        res : Box
            The extended box including the ruler.
        """
        x1 = self._line_dbu.p1.x
        y1 = self._line_dbu.p1.y
        x2 = self._line_dbu.p2.x
        y2 = self._line_dbu.p2.y
        if x2 < x1:
            x1, x2 = x2, x1

        if y2 < y1:
            y1, y2 = y2, y1

        x1 -= self._extend
        y1 -= self._extend
        x2 += self._extend
        y2 += self._extend
        return Box(Point(x1 - self._delta * 5, y1 - self._delta * 5),
                   Point(x2 + self._delta * 5, y2 + self._delta * 5))

    @property
    def dbu(self):
        return self._dbu

    def layers_file(self, lyp_file):
        """Configures a .lyp layer properties file to be used on the cross-section layout

        """
        self._lyp_file = lyp_file

    # The basic generation method
    def run(self, p1, p2, ruler_text=''):
        """

        Returns
        -------
        LayoutView
        """
        self._target_view = None

        self._setup(p1, p2, xs_name=ruler_text)

        self._update_basic_regions()

        text = None
        with open(self._file_path) as file:
            text = file.read()

        if not text:
            MessageBox.critical("Error",
                                "Error reading file #{self._file_path}",
                                MessageBox.b_ok())
            return None

        # prepare variables to be visible in the script
        locals_ = dir(self)
        locals_dict = {}
        for attr in locals_:
            if attr[0] != '_':
                locals_dict.update({attr: getattr(self, attr)})

        try:
            exec(text, locals_dict)
        except Exception as e:
            # For development
            # print(e.__traceback__.)
            # print(dir(e))
            MessageBox.critical("Error", str(e), MessageBox.b_ok())
            # pass
            return None

        Application.instance().main_window().cm_lv_add_missing()  # @@@
        if self._lyp_file:
            self._target_view.load_layer_props(self._lyp_file)
        self._target_view.zoom_fit()
        return self._target_view

    @print_info(False)
    def _xpoints_to_mask(self, iv):
        """ Convert crossing points to a mask

        Parameters
        ----------
        iv : list of lists or list of tuple
            each list / tuple represents two coordinates.

        Return
        ------
        res : MaterialData
            Top ot the surface for deposition
        """
        info('    iv = {})'.format(iv))
        s = 0
        last_s = 0
        p1 = 0
        p2 = 0

        mask_polygons = []
        for i in iv:
            z = i[0]  # first coordinate
            s += i[1]  # increase second coordinate

            if last_s <= 0 < s:  # s increased and became > 0
                p1 = z
            elif last_s > 0 >= s:  # s decreased and became < 0
                p2 = z
                poly = Polygon(Box(p1, -self._depth - self._below,
                                   p2, self._height))
                info('        Appending poly {}'.format(poly))
                mask_polygons.append(poly)
            last_s = s

        info('    mask_polys = {}'.format(mask_polygons))

        '''
        air = self._air.data
        info('    air =        {}'.format(air))

        # Sizing is needed only in vertical direction, it seems
        # air_sized = self._ep.size_p2p(air, self._delta, self._delta)
        air_sized = self._ep.size_p2p(air, self._delta, self._delta)
        info('    air_sized =  {}'.format(air_sized))

        # extended air minus air
        air_border = self._ep.boolean_p2p(air_sized, air, EP.ModeANotB)
        info('    air_border = {}'.format(air_border))

        # overlap of air border and mask polygons
        mask_data = self._ep.boolean_p2p(
                air_border, mask_polygons,
                EP.ModeAnd)
        info('    mask_data  = {}'.format(mask_data))

        # info('____Creating MD from {}'.format([str(p) for p in mask_data]))
        return MaterialData(mask_data, self)
        '''
        info('Before MaskData creation')
        res = MaskData(self._air.data, mask_polygons, self)
        info('res = {}'.format(res))
        return res

    @print_info(False)
    def _update_basic_regions(self):

        h = self._height  # height above the wafer
        d = self._depth  # thickness of the wafer
        b = self._below  # distance below the wafer

        w = self._line_dbu.length()  # length of the ruler
        e = self._extend  # extend to the sides

        self._area = Box(-e, -(d+b), w+e, h)
        self._air = MaterialData([Polygon(Box(-e, 0, w + e, h))], self)
        self._air_below = MaterialData([Polygon(Box(-e, -(d+b), w+e, -d))],
                                       self)

        self._bulk = MaterialData([Polygon(Box(-e, -d, w+e, 0))], self)
        self._roi = Box(0, -(d+b), w, h)

        info('    XSG._area:      {}'.format(self._area))
        info('    XSG._roi:       {}'.format(self._roi))
        info('    XSG._air:       {}'.format(self._air))
        info('    XSG._bulk:      {}'.format(self._bulk))
        info('    XSG._air_below: {}'.format(self._air_below))

    @print_info(False)
    def _setup(self, p1, p2, xs_name=None):
        """
        Parameters
        ----------
        p1 : Point
            first point of the ruler
        p2 : Point
            second point of the ruler
        xs_name : str
            identifier to be used to name a new cross-section cell
        """
        # locate the layout
        app = Application.instance()
        view = app.main_window().current_view()  # LayoutView
        if not view:
            MessageBox.critical(
                    "Error", "No view open for creating the cross-"
                    "section from", MessageBox.b_ok())
            return False

        cv = view.cellview(view.active_cellview_index())  # CellView
        if not cv.is_valid():
            MessageBox.critical("Error",
                                "The selected layout is not valid",
                                MessageBox.b_ok())
            return False

        self._cv = cv  # CellView
        self._layout = cv.layout()  # Layout
        self._dbu = self._layout.dbu
        self._cell = cv.cell_index  # int

        # get the start and end points in database units and micron
        p1_dbu = Point.from_dpoint(p1 * (1.0 / self._dbu))
        p2_dbu = Point.from_dpoint(p2 * (1.0 / self._dbu))
        self._line_dbu = Edge(p1_dbu, p2_dbu)  # Edge describing the ruler

        cell_name = "PYXS: "+xs_name if xs_name else "XSECTION"

        # create a new layout for the output
        cv = app.main_window().create_layout(1)  # type: CellView
        cell = cv.layout().add_cell(cell_name)  # type: Cell
        self._target_view = app.main_window().current_view()  # type: LayoutView
        self._target_view.select_cell(cell, 0)
        self._target_layout = cv.layout()  # type: Layout
        self._target_layout.dbu = self._dbu
        self._target_cell = cell  # type: cell

        # initialize height and depth
        self._extend = int_floor(2.0 / self._dbu + 0.5)  # 2 um in dbu
        self._delta = 10
        self._height = int_floor(2.0 / self._dbu + 0.5)  # 2 um in dbu
        self._depth = int_floor(2.0 / self._dbu + 0.5)  # 2 um in dbu
        self._below = int_floor(2.0 / self._dbu + 0.5)  # 2 um in dbu

        info('    XSG._dbu is:    {}'.format(self._dbu))
        info('    XSG._extend is: {}'.format(self._extend))
        info('    XSG._delta is:  {}'.format(self._delta))
        info('    XSG._height is: {}'.format(self._height))
        info('    XSG._depth is:  {}'.format(self._depth))
        info('    XSG._below is:  {}'.format(self._below))

        return True


# MENU AND ACTIONS
# ----------------
N_PYXS_SCRIPTS_MAX = 4

pyxs_script_load_menuhandler = None
pyxs_scripts = None


class MenuHandler(Action):
    """ Handler for the load .xs file action
    """
    def __init__(self, title, action, shortcut=None, icon=None):
        """
        Parameters
        ----------
        title : str
        action : callable
        shortcut : str
        icon : str
        """
        self.title = title
        self._action = action
        if shortcut:
            self.shortcut = shortcut
        if icon:
            self.icon = icon

    def triggered(self):
        self._action()


class XSectionMRUAction(Action):
    """ A special action to implement the cross section MRU menu item
    """

    def __init__(self, action):
        """
        Parameters
        ----------
        action : callable
        """
        self._action = action
        self._script = None
        # self.title = None
        # self.visible = False

    def triggered(self):
        self._action(self.script)

    @property
    def script(self):
        return self._script

    @script.setter
    def script(self, s):
        self._script = s
        self.visible = (s is not None)
        if s:
            self.title = os.path.basename(s)


class XSectionScriptEnvironment(object):
    """ The cross section script environment
    """
    def __init__(self):
        app = Application.instance()
        mw = app.main_window()
        if mw is None:
            return

        def _on_triggered_callback():
            """ Load pyxs script menu action.

            Load new .pyxs file and run it.
            """
            view = Application.instance().main_window().current_view()
            if not view:
                raise UserWarning("No view open for running the pyxs script")

            filename = FileDialog.get_open_file_name(
                    "Select cross-section script", "",
                    "XSection Scripts (*.pyxs);;All Files (*)")

            # run the script and save it
            if filename.has_value():
                self.run_script(filename.value())
                self.make_mru(filename.value())

        def _XSectionMRUAction_callback(script):
            """ *.pyxs menu action

            Load selected .pyxs file and run it.

            Parameters
            ----------
            script : str
            """
            self.run_script(script)
            self.make_mru(script)

        # Create pyxs submenu in Tools
        menu = mw.menu()
        if not menu.is_valid("tools_menu.pyxs_script_group"):
            menu.insert_separator("tools_menu.end", "pyxs_script_group")
            menu.insert_menu("tools_menu.end", "pyxs_script_submenu", "pyxs")

        # Create Load XSection.py Script item in XSection (py)
        global pyxs_script_load_menuhandler
        pyxs_script_load_menuhandler = MenuHandler(
                "Load pyxs script", _on_triggered_callback)
        menu.insert_item("tools_menu.pyxs_script_submenu.end",
                         "pyxs_script_load", pyxs_script_load_menuhandler)
        menu.insert_separator("tools_menu.pyxs_script_submenu.end.end",
                              "pyxs_script_mru_group")

        # Create list of existing pyxs scripts item in pyxs
        self._mru_actions = []
        for i in range(N_PYXS_SCRIPTS_MAX):
            a = XSectionMRUAction(_XSectionMRUAction_callback)
            self._mru_actions.append(a)
            menu.insert_item("tools_menu.pyxs_script_submenu.end",
                             "pyxs_script_mru{}".format(i), a)
            a.script = None

        # try to save the MRU list to $HOME/.klayout-processing-mru
        i = 0
        home = os.getenv("HOME", None) or os.getenv("HOMESHARE", None)
        global pyxs_scripts
        if pyxs_scripts:
            for i, script in enumerate(pyxs_scripts.split(":")):
                if i < len(self._mru_actions):
                    self._mru_actions[i].script = script
        elif home:
            fn = home + "\\.klayout-pyxs-scripts"
            try:
                with open(fn, "r") as file:
                    for line in file.readlines():
                        match = re.match('<mru>(.*)<\/mru>', line)
                        if match:
                            if i < len(self._mru_actions):
                                self._mru_actions[i].script = match.group(1)
                            i += 1
            except:
                pass

    def run_script(self, filename, p1=None, p2=None):
        """ Run .pyxs script

        filename : str
            path to the .pyxs script
        """
        view = Application.instance().main_window().current_view()
        if not view:
            raise UserWarning("No view open for running the pyxs script")

        if p1 is None or p2 is None:
            app = Application.instance()
            scr_view = app.main_window().current_view()  # type: LayoutView
            scr_view_idx = app.main_window().current_view_index
            if not scr_view:
                MessageBox.critical(
                    "Error", "No view open for creating the cross-"
                             "section from", MessageBox.b_ok())
                return False

            rulers = []

            for a in scr_view.each_annotation():
                rulers.append(a)

            if len(rulers) == 0:
                MessageBox.critical("Error",
                                    "No ruler present for the cross "
                                    "section line", MessageBox.b_ok())
                return None

            p1_arr, p2_arr, ruler_text_arr = [], [], []

            for ruler in rulers:
                p1_arr.append(ruler.p1)
                p2_arr.append(ruler.p2)
                ruler_text_arr.append(ruler.text())

        else:
            p1_arr, p2_arr, ruler_text_arr = [p1], [p2], ['']
            scr_view_idx = None

        target_views = []
        for p1_, p2_, text_ in zip(p1_arr, p2_arr, ruler_text_arr):
            if scr_view_idx:
                # return to the original view to run it again
                app.main_window().select_view(scr_view_idx)
            view = XSectionGenerator(filename).run(p1_, p2_, text_)
            target_views.append(view)

        return target_views
        # try:
        #     # print('XSectionGenerator(filename).run()')
        #     XSectionGenerator(filename).run()
        # except Exception as e:
        #     MessageBox.critical("Script failed", str(e),
        #                             MessageBox.b_ok())

    def make_mru(self, script):
        """ Save list of scripts

        script : str
            path to the script to be saved
        """
        # Don't maintain MRU if an external list is provided
        global pyxs_scripts
        if pyxs_scripts:
            return

        # Make a new script list. New script goes first, ...
        scripts = [script]
        # ... the rest are taken from the existing list
        for a in self._mru_actions:
            if a.script != script:
                scripts.append(a.script)

        # make sure the list is filled to the same length
        while len(scripts) < len(self._mru_actions):
            scripts.append(None)

        # update list of actions
        for i in range(len(self._mru_actions)):
            self._mru_actions[i].script = scripts[i]

        # try to save the MRU list to $HOME/.klayout-xsection
        home = os.getenv("HOME", None) or os.getenv("HOMESHARE", None)
        if home:
            fn = home + "\\.klayout-pyxs-scripts"
            with open(fn, "w") as file:
                file.write("<pyxs>\n")
                for a in self._mru_actions:
                    if a.script:
                        file.write("<mru>{}</mru>\n".format(a.script))
                file.write("</pyxs>\n")


if __name__ == '__main__':
    import doctest
    doctest.testmod()
