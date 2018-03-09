from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import sys
import logging
from functools import reduce

import numpy as np
import pyqtgraph as pg
from itertools import cycle

from astropy.units import Quantity

from qtpy.QtWidgets import (QMainWindow, QVBoxLayout, QHBoxLayout, QLabel,
                            QLineEdit, QPushButton, QWidget)
from qtpy.QtCore import QEvent, Qt, QThread, Signal, QMutex, QPoint

from ..core.events import dispatch
from ..core.linelist import ingest, LineList, \
    REDSHIFTED_WAVELENGTH_COLUMN, MARKER_COLUMN, ID_COLUMN, COLOR_COLUMN, HEIGHT_COLUMN

from ..core.plots import LinePlot
from ..core.annotation import LineIDMarker
from .axes import DynamicAxisItem
from .region_items import LinearRegionItem
from .dialogs import ResampleDialog
from ..analysis.utils import resample

from .linelists_window import LineListsWindow


pg.setConfigOption('background', 'w')
pg.setConfigOption('foreground', 'k')
pg.setConfigOptions(antialias=False)


AVAILABLE_COLORS = [
    (0, 0, 0),
    (0.2980392156862745, 0.4470588235294118, 0.6901960784313725),
    (0.3333333333333333, 0.6588235294117647, 0.40784313725490196),
    (0.7686274509803922, 0.3058823529411765, 0.3215686274509804),
    (0.5058823529411764, 0.4470588235294118, 0.6980392156862745),
    (0.8, 0.7254901960784313, 0.4549019607843137),
    (0.39215686274509803, 0.7098039215686275, 0.803921568627451),
    (0.2980392156862745, 0.4470588235294118, 0.6901960784313725),
    (0.3333333333333333, 0.6588235294117647, 0.40784313725490196),
    (0.7686274509803922, 0.3058823529411765, 0.3215686274509804),
    (0.5058823529411764, 0.4470588235294118, 0.6980392156862745)
]


class UiPlotSubWindow(QMainWindow):
    """
    Main plotting window.
    """
    def __init__(self, *args, **kwargs):
        super(UiPlotSubWindow, self).__init__(*args, **kwargs)

        self.vertical_layout = QVBoxLayout()
        self.horizontal_layout = QHBoxLayout()
        self.vertical_layout.setContentsMargins(0, 0, 0, 0)
        self.vertical_layout.setSpacing(2)

        # X range
        self.label_x_range = QLabel()
        self.label_x_range.setText("X Range")
        self.line_edit_min_x_range = QLineEdit()
        self.line_edit_max_x_range = QLineEdit()

        self.layout_x_range = QHBoxLayout()
        self.layout_x_range.addWidget(self.label_x_range)
        self.layout_x_range.addWidget(self.line_edit_min_x_range)
        self.layout_x_range.addWidget(self.line_edit_max_x_range)

        # Y range
        self.label_y_range = QLabel()
        self.label_y_range.setText("Y Range")
        self.line_edit_min_y_range = QLineEdit()
        self.line_edit_max_y_range = QLineEdit()

        self.layout_y_range = QHBoxLayout()
        self.layout_y_range.addWidget(self.label_y_range)
        self.layout_y_range.addWidget(self.line_edit_min_y_range)
        self.layout_y_range.addWidget(self.line_edit_max_y_range)

        # Reset
        self.button_reset = QPushButton()
        self.button_reset.setText("Reset")

        # Cursor position
        self.line_edit_cursor_pos = QLabel()
        # self.line_edit_cursor_pos.setReadOnly(True)
        self.line_edit_cursor_pos.setText("Pos: 0, 0")

        # Line labels
        self._linelist_window = None

        self.horizontal_layout.addWidget(self.line_edit_cursor_pos)
        self.horizontal_layout.addStretch()
        # self.horizontal_layout.addLayout(self.layout_x_range)
        # self.horizontal_layout.addLayout(self.layout_y_range)
        self.horizontal_layout.addWidget(self.button_reset)

        self.vertical_layout.addLayout(self.horizontal_layout)

        self.main_widget = QWidget(self)
        self.main_widget.setLayout(self.vertical_layout)

        self.setCentralWidget(self.main_widget)


class PlotSubWindow(UiPlotSubWindow):
    """
    Sub window object responsible for displaying and interacting with plots.
    """
    def __init__(self, *args, **kwargs):
        super(PlotSubWindow, self).__init__(*args, **kwargs)
        self._plots = []
        self._dynamic_axis = None
        self._plot_widget = None
        self._plot_item = None
        self._plots_units = None
        self._rois = []
        self._measure_rois = []
        self._centroid_roi = None
        self._is_selected = True
        self._layer_items = []
        self.disable_errors = False
        self.disable_mask = True

        dispatch.setup(self)

        self._dynamic_axis = DynamicAxisItem(orientation='top')
        self._plot_widget = pg.PlotWidget(
            axisItems={'top': self._dynamic_axis})
        # self.setCentralWidget(self._plot_widget)
        self.vertical_layout.insertWidget(0, self._plot_widget)

        self._plot_item = self._plot_widget.getPlotItem()
        self._plot_item.showAxis('top', True)
        # Add grids to the plot
        self._plot_item.showGrid(True, True)

        self._setup_connections()

        self.linelists = []

        # Initial color list for this sub window
        self._available_colors = cycle(AVAILABLE_COLORS)

        # Incorporate event filter
        self.installEventFilter(self)

        # Create a single vertical line object that can be used to indicate
        # wavelength position
        self._disp_line = pg.InfiniteLine(movable=True, pen={'color': 'g'})
        self._plot_item.addItem(self._disp_line)

        # When the user moves the dispersion vertical line, send an event
        self._disp_line.sigPositionChanged.connect(lambda:
            dispatch.change_dispersion_position.emit(
                pos=self._disp_line.value()))

    def _setup_connections(self):
        # Connect cursor position to UI element
        # proxy = pg.SignalProxy(self._plot_item.scene().sigMouseMoved,
        #                        rateLimit=30, slot=self.cursor_moved)
        self._plot_item.scene().sigMouseMoved.connect(self.cursor_moved)
        self.button_reset.clicked.connect(self._reset_view)

        self._plot_widget.sigYRangeChanged.connect(self._process_zoom_signal)

    @dispatch.register_listener("changed_dispersion_position")
    def _move_vertical_line(self, pos):
        # Get the actual dispersion value from the index provided
        disp = 0

        try:
            disp = self._plots[0].layer.dispersion[pos]
        except IndexError:
            logging.error("No available plots from which to get dispersion"
                          "position.")

        self._disp_line.setValue(disp)

    def cursor_moved(self, evt):
        pos = evt

        # Data range
        # flux = self._containers[0].data.value
        # disp = self._containers[0].dispersion.value

        # Plot range
        vb = self._plot_item.getViewBox()

        if self._plot_item.sceneBoundingRect().contains(pos):
            mouse_point = vb.mapSceneToView(pos)
            index = int(mouse_point.x())

            if index >= 0 and index < vb.viewRange()[0][1]:
                self.line_edit_cursor_pos.setText("Pos: {0:4.4g}, "
                                                  "{1:4.4g}".format(
                    mouse_point.x(), mouse_point.y())
                    # flux[index], disp[index])
                )

    def _reset_view(self):
        view_box = self._plot_item.getViewBox()
        view_box.autoRange()

    def get_roi_mask(self, layer=None, container=None, roi=None):
        if layer is not None:
            container = self.get_plot(layer)

        if container is None:
            return

        mask = np.ones(layer.masked_dispersion.shape, dtype=bool)
        mask_holder = []
        rois = [roi] if roi is not None else self._rois

        for roi in rois:
            # roi_shape = roi.parentBounds()
            # x1, y1, x2, y2 = roi_shape.getCoords()
            x1, x2 = roi.getRegion()

            mask = (container.layer.masked_dispersion.data.value >= x1) & \
                   (container.layer.masked_dispersion.data.value <= x2)

            mask_holder.append(mask)

        if len(mask_holder) > 0:
            mask = reduce(np.logical_or, mask_holder)
            mask = reduce(np.logical_and, [container.layer.layer_mask, mask])

        return mask

    def eventFilter(self, widget, event):
        if (event.type() == QEvent.KeyPress):
            key = event.key()

            if key == Qt.Key_Delete or key == Qt.Key_Backspace:
                for roi in self._rois:
                    if roi.mouseHovering:
                        roi.sigRemoveRequested.emit(roi)

                return True

        return QWidget.eventFilter(self, widget, event)

    def add_roi(self, bounds=None, *args, **kwargs):
        if bounds is None:
            view_range = self._plot_item.viewRange()
            x_len = (view_range[0][1] - view_range[0][0]) * 0.5
            x_pos = x_len * 0.5 + view_range[0][0]
            start, stop = x_pos, x_pos + x_len
        else:
            start, stop = bounds

        def remove():
            self._plot_item.removeItem(roi)
            self._rois.remove(roi)
            dispatch.removed_roi.emit(roi=roi)

        roi = LinearRegionItem(values=[start, stop])
        self._rois.append(roi)
        self._plot_item.addItem(roi)

        # Connect the remove functionality
        roi.sigRemoveRequested.connect(remove)

        # Connect events
        dispatch.on_updated_rois.emit(rois=self._rois)
        roi.sigRemoveRequested.connect(
            lambda: dispatch.on_updated_rois.emit(rois=self._rois))
        roi.sigRegionChangeFinished.connect(
            lambda: dispatch.on_updated_rois.emit(rois=self._rois))
        roi.sigRegionChangeFinished.connect(
            lambda: dispatch.changed_roi_mask.emit(
                mask=self.get_roi_mask(layer=self.get_all_layers()[0])))

        # Signal that an ROI has been created and added to plot
        dispatch.added_roi.emit(roi=roi)

    def get_roi_bounds(self):
        bounds = []

        for roi in self._rois:
            # roi_shape = roi.parentBounds()
            # x1, y1, x2, y2 = roi_shape.getCoords()
            bounds.append(list(roi.getRegion()))

        return bounds

    def get_plot(self, layer):
        for plot in self._plots:
            if plot.layer == layer:
                return plot

    def get_all_layers(self):
        return [plot.layer for plot in self._plots]

    @dispatch.register_listener("changed_units")
    def change_units(self, x=None, y=None, z=None):
        for cntr in self._plots:
            cntr.change_units(x, y, z)

        self.set_labels(x_label=x, y_label=y)
        self._plot_item.enableAutoRange()
        self._plot_units = [x, y, z]

    def set_labels(self, x_label='', y_label=''):
        self._plot_item.setLabels(
            left="Flux [{}]".format(
                y_label or str(self._plots[0].layer.unit)),
            bottom="Wavelength [{}]".format(
                x_label or str(self._plots[0].layer.dispersion_unit)))

    def set_visibility(self, layer, show_data, show_uncert, show_masked, inactive=None):
        plot = self.get_plot(layer)

        if plot is not None:
            plot.set_plot_visibility(show_data, inactive=inactive)
            plot.set_error_visibility(show_uncert)
            plot.set_mask_visibility(show_masked)

    def set_plot_style(self, layer, mode=None, line_width=None):
        plot = self.get_plot(layer)

        if mode is not None:
            plot.set_mode(mode)

        if line_width is not None:
            plot.set_line_width(line_width)

    def update_axis(self, layer=None, mode=None, **kwargs):
        self._dynamic_axis.update_axis(layer, mode, **kwargs)
        self._plot_widget.update()

    def update_plot_item(self):
        self._plot_item.update()

    @dispatch.register_listener("on_update_model")
    def update_plot(self, layer=None, plot=None):
        if layer is not None:
            plot = self.get_plot(layer)

            plot.update()

    def closeEvent(self, event):

        # before tearing down event handlers, need to close
        # any line lists window that might be still open.
        dispatch.on_dismiss_linelists_window.emit()

        dispatch.tear_down(self)
        super(PlotSubWindow, self).closeEvent(event)

    @dispatch.register_listener("on_add_layer")
    def add_plot(self, layer, window=None, style=None):
        if window is not None and window != self:
            return

        # Make sure the new plot layer has the same sampling as the current
        # layers
        def comp_disp(plot, layer):
            lstep = np.mean(layer.masked_dispersion[1:] - layer.masked_dispersion[:-1])
            pstep = np.mean(plot.layer.masked_dispersion[1:] -
                            plot.layer.masked_dispersion[:-1])

            return np.isclose(lstep.value, pstep.value)

        if not all(map(lambda p: comp_disp(p, layer=layer), self._plots)):
            logging.warning("New layer '{}' does not have the same dispersion "
                          "as current plot data.".format(layer.name))

            resample_dialog = ResampleDialog()

            if resample_dialog.exec_():
                in_data = np.ones(layer.masked_dispersion.shape,
                                  [('wave', float),
                                   ('data', float),
                                   ('err', float)])

                in_data['wave'] = layer.masked_dispersion.data.value
                in_data['data'] = layer.masked_data.data.value
                in_data['err'] = layer.uncertainty.array

                plot = self._plots[0]

                out_data = resample(in_data, 'wave', plot.layer.masked_dispersion.data.value,
                                    ('data', 'err'),
                                    kind=resample_dialog.method)

                new_data = layer.copy(
                    layer,
                    data=out_data['data'],
                    dispersion=out_data['wave'],
                    uncertainty=layer.uncertainty.__class__(out_data['err']),
                    dispersion_unit=layer.dispersion_unit)

                layer = layer.from_parent(
                    new_data,
                    name="Interpolated {}".format(layer.name),
                    layer_mask=layer.layer_mask)

        new_plot = LinePlot.from_layer(
            layer, **(style or {'color': next(self._available_colors)}))

        if len(self._plots) == 0:
            is_convert_success = self.change_units(
                new_plot.layer.dispersion_unit, new_plot.layer.unit)
        else:
            is_convert_success = new_plot.change_units(*self._plot_units)

            if not is_convert_success[0] or not is_convert_success[1]:
                logging.error("Unable to convert {} axis units of '{}' to current plot"
                              " units.".format('x' if not is_convert_success[0] else 'y',
                                               new_plot.layer.name))

                dispatch.on_remove_layer.emit(layer=layer)
                return

        if new_plot.error is not None:
            self._plot_item.addItem(new_plot.error)

        if new_plot.mask is not None:
            self._plot_item.addItem(new_plot.mask)

        self._plots.append(new_plot)
        self._plot_item.addItem(new_plot.plot)

        self.set_active_plot(new_plot.layer)

        # Make sure the dynamic axis object has access to a layer
        self._dynamic_axis._layer = self._plots[0].layer
        dispatch.on_added_layer.emit(layer=layer)
        dispatch.on_added_plot.emit(plot=new_plot, window=window)

    @dispatch.register_listener("on_removed_layer")
    def remove_plot(self, layer, window=None):
        if window is not None and window != self:
            return

        for plot in self._plots:
            if plot.layer == layer:
                self._plot_item.removeItem(plot.plot)

                if plot.error is not None:
                    self._plot_item.removeItem(plot.error)

                if plot.mask is not None:
                    self._plot_item.removeItem(plot.mask)

                self._plots.remove(plot)

    def set_active_plot(self, layer, checked_state=None):
        for plot in self._plots:
            if plot.checked:
                if plot.layer == layer:
                    self.set_visibility(plot.layer, True, not self.disable_errors, not self.disable_mask)
                else:
                    self.set_visibility(plot.layer, True, False, False)
            else:
                self.set_visibility(plot.layer, False, False, False)


#--------  Line lists and line labels handling.

    # Finds the wavelength range spanned by the spectrum (or spectra)
    # at hand. The range will be used to bracket the set of lines
    # actually read from the line list table(s).
    def _find_wavelength_range(self):
        # increasing dispersion values!
        amin = sys.float_info.max
        amax = 0.0

        for container in self._plots:
            amin = min(amin, container.layer.masked_dispersion.compressed().value[0])
            amax = max(amax, container.layer.masked_dispersion.compressed().value[-1])

        amin = Quantity(amin, self._plot_units[0])
        amax = Quantity(amax, self._plot_units[0])

        return (amin, amax)

    # Buffering of zoom events.
    def _process_zoom_signal(self):
        # if hasattr(self, '_zoom_markers_thread') and self._zoom_markers_thread:

            data_range = self._plot_item.vb.viewRange()
            xmin = data_range[0][0]
            xmax = data_range[0][1]
            ymin = data_range[1][0]
            ymax = data_range[1][1]

            # self._zoom_event_buffer.access((ymin, ymax), put=True)
            self.handle_zoom((xmin, xmax), (ymin, ymax))

    def handle_zoom(self, xrange, yrange):
        # this method may be called by zoom signals that can be emitted
        # when a merged line list is not available yet.
        if hasattr(self, '_merged_linelist') and self._is_displaying_markers:

            # prevent burst calls to step into each other.
            self._plot_widget.sigYRangeChanged.disconnect(self._process_zoom_signal)

            # update height column in line list based on
            # the new, zoomed coordinates.
            height_array = self._compute_height(self._merged_linelist, yrange)

            marker_column = self._merged_linelist[MARKER_COLUMN]
            for row_index in range(len(marker_column)):
                marker = marker_column[row_index]

                self._plot_item.removeItem(marker)

                new_marker = LineIDMarker(marker=marker)
                new_marker.setPos(marker.x(), height_array[row_index])

                # self._plot_item.addItem(new_marker)
                marker_column[row_index] = new_marker

            self._clean_markers(marker_column, xrange)

            # add markers to plot AFTER the markers get cleaned
            for marker in marker_column:
                self._plot_item.addItem(marker)

            self._plot_item.update()

            self._plot_widget.sigYRangeChanged.connect(self._process_zoom_signal)

    @dispatch.register_listener("on_request_linelists")
    def _request_linelists(self, *args, **kwargs):
        self.waverange = self._find_wavelength_range()

        self.linelists = ingest(self.waverange)

    def _go_plot_markers(self, merged_linelist):
        # Code below is plotting all markers at a fixed height in
        # the screen coordinate system. Still TBD how to do this in
        # the generic case. Maybe derive heights from curve data
        # instead? Make the markers follow the curve ups and downs?
        #
        # Ideally we would like to have the marker's X coordinate
        # pinned down to the plot surface in data value, and the Y
        # coordinate pinned down in screen value. This would make
        # the markers to stay at the same height in the window even
        # when the plot is zoomed. The elegant nice way to do this
        # would be via the marker objects themselves to reposition
        # themselves on screen. This did not work though. There seems
        # to be a clash (maybe thread-related) in between the setPos
        # method and the auto-range facility in pyqtgraph.
        #
        # We managed to get the pinning in Y by brute force: remove
        # the markers and re-draw them in the new zoomed coordinates.
        # This is handled by the handle_zoom method that in turn relies
        # in the storage of markers row-wise in the line list table.
        # curve = plot_item.curves[0]

        plot_item = self._plot_item

        data_range = plot_item.vb.viewRange()
        ymin = data_range[1][0]
        ymax = data_range[1][1]

        height = self._compute_height(merged_linelist, (ymin, ymax))

        # column names are defined in the YAML files
        # or by constants elsewhere.
        wave_column = merged_linelist.columns[REDSHIFTED_WAVELENGTH_COLUMN]
        id_column = merged_linelist.columns[ID_COLUMN]
        color_column = merged_linelist[COLOR_COLUMN]

        # To enable marker removal from plot, markers are stored
        # row-wise so as to match row selections in table views.
        marker_column = merged_linelist[MARKER_COLUMN]

        # plot_item.enableAutoRange(enable=False)
        for row_index in range(len(wave_column)):

            # tool tip contains all info in table.
            tool_tip = ""
            for col_index in range(len(merged_linelist.columns)):
                col_name = merged_linelist.colnames[col_index]
                if not col_name in [COLOR_COLUMN]:
                    value = merged_linelist.columns[col_index][row_index]
                    tool_tip += col_name + '=' + str(value) + ', '

            marker = LineIDMarker(text=id_column[row_index],
                                  plot_item=plot_item,
                                  tip=tool_tip,
                                  color=color_column[row_index],
                                  orientation='vertical')

            marker.setPos(wave_column[row_index], height[row_index])

            # plot_item.addItem(marker)
            marker_column[row_index] = marker

        # find out about packing on screen.
        xmin = data_range[0][0]
        xmax = data_range[0][1]
        self._clean_markers(marker_column, (xmin, xmax))

        # add markers to plot AFTER the markers get cleaned
        for marker in marker_column:
            self._plot_item.addItem(marker)

        plot_item.update()

    def _compute_height(self, merged_linelist, yrange):
        ymin = yrange[0]
        ymax = yrange[1]

        return (ymax - ymin) * merged_linelist.columns[HEIGHT_COLUMN] + ymin

    def _clean_markers(self, markers, xrange):
        xmin = xrange[0]
        xmax = xrange[1]
        xspan = xmax - xmin

        # for index in range(1, len(markers)):
            # x1 = markers[index-1].x()
            # x2 = markers[index].x()
            # p1 = (x1 - xmin) / xspan
            # p2 = (x2 - xmin) / xspan
            # if (p2 - p1) < 200:
            #     markers[index].short_form = True
            # else:
            #     markers[index].short_form = False


        x = np.array([marker.x() for marker in markers])


        # p = (x - xmin) / xspan
        p = (x - xmin)
        differences = np.diff(p)


        # for marker, difference in zip(markers, differences):
        #     marker.short_form = difference < 20


    @dispatch.register_listener("on_plot_linelists")
    def _plot_linelists(self, table_views, panes, units, **kwargs):

        if not self._is_selected:
            return

        # Get a list of the selected indices in each line list.
        # Build new line lists with only the selected rows.
        linelists_with_selections = []

        self._remove_linelabels_from_plot()

        for table_view, pane in zip(table_views, panes):
            # Find matching line list by its name. This could be made
            # simpler by the use of a dict. That breaks code elsewhere
            # though: it is assumed by that code that self.linelists
            # is a list and not a dict.
            view_name = table_view.model().getName()
            for k in range(len(self.linelists)):
                line_list = self.linelists[k]
                line_list_name = line_list.name

                if line_list_name == view_name:
                    # must map between view and underlying model
                    # because of row sorting.
                    selected_rows = table_view.selectionModel().selectedRows()
                    model_selected_rows = []
                    for sr in selected_rows:
                        model_row = table_view.model().mapToSource(sr)
                        model_selected_rows.append(model_row)

                    new_list = line_list.extract_rows(model_selected_rows)

                    # redshift correction for plotting the specific lines
                    # defined in this list. Defined by the text content
                    # and combo box setting.
                    if pane.redshift_textbox.hasAcceptableInput():
                        redshift = float(pane.redshift_textbox.text())
                        z_units = pane.combo_box_z_units.currentText()
                        new_list.setRedshift(redshift, z_units)

                    # color for plotting the specific lines defined in
                    # this list, is defined by the itemData property.
                    index = pane.combo_box_color.currentIndex()
                    color = pane.combo_box_color.itemData(index, role=Qt.UserRole)
                    new_list.setColor(color)

                    # height for plotting the specific lines defined in
                    # this list. Defined by the line edit text.
                    if pane.height_textbox.hasAcceptableInput():
                        height = float(pane.height_textbox.text())
                        new_list.setHeight(height)

                    linelists_with_selections.append(new_list)

        # Merge all line lists into a single one.
        merged_linelist = LineList.merge(linelists_with_selections, units)

        self._go_plot_markers(merged_linelist)
        self._is_displaying_markers = True
        # self._zoom_event_buffer = ZoomEventBuffer()
        # self._zoom_markers_thread = ZoomMarkersThread(self)
        # self._zoom_markers_thread.start()

        self._linelist_window.displayPlottedLines(merged_linelist)

        self._merged_linelist = merged_linelist

    @dispatch.register_listener("on_erase_linelabels")
    def erase_linelabels(self, *args, **kwargs):
        if self._linelist_window and self._is_selected:

            self._is_displaying_markers = False
            # self._zoom_markers_thread = None

            self._remove_linelabels_from_plot()
            self._linelist_window.erasePlottedLines()

    def _remove_linelabels_from_plot(self):
        if hasattr(self, '_merged_linelist'):
            marker_column = self._merged_linelist[MARKER_COLUMN]
            for index in range(len(marker_column)):
                self._plot_item.removeItem(marker_column[index])
            self._plot_item.update()

    # The 3 handlers below, and their associated signals, implement
    # the logic that defines the show/hide/dismiss behavior of the
    # line list window. It remains to be seen if it is what users
    # actually want.

    @dispatch.register_listener("on_activated_window")
    def _set_selection_state(self, window):
        self._is_selected = window == self

        if self._linelist_window:
            if self._is_selected:
                self._linelist_window.show()
            else:
                self._linelist_window.hide()

    @dispatch.register_listener("on_show_linelists_window")
    def _show_linelists_window(self, *args, **kwargs):
        if self._is_selected:
            if self._linelist_window is None:
                self._linelist_window = LineListsWindow(self)
            self._linelist_window.show()

    @dispatch.register_listener("on_dismiss_linelists_window")
    def _dismiss_linelists_window(self, *args, **kwargs):
        if self._is_selected and self._linelist_window:
            self._linelist_window.hide()
            self._linelist_window = None


class ZoomMarkersThread(QThread):

    def __init__(self, caller):
        super(ZoomMarkersThread, self).__init__()

        self.caller = caller
        self.mutex = QMutex()

        self._buffer = self.caller._zoom_event_buffer

    def __del__(self):
        self.wait()

    def run(self):
        self.mutex.lock()

        while(1):
            if len(self._buffer.buffer) > 0:

                yrange = self._buffer.buffer.pop()

                print("@@@@@@  file sub_windows.py; line 737 - ",  yrange)

                self.caller.handle_zoom(yrange)

            # QThread.sleep(1)

        self.mutex.unlock()


class ZoomEventBuffer(object):

    def __init__(self):
        self.buffer = []
        self.mutex = QMutex()

    # we need all access to the internal buffer
    # to be under control of a single mutex.
    def access(self, value=None, put=False):
        self.mutex.lock()

        if put:
            # store in buffer
            self.buffer.insert(0, value)
        else:
            # get from buffer
            if len(self.buffer) > 0:
                value = self.buffer.pop()
            else:
                value = None

        self.mutex.unlock()

        print("@@@@@@  file sub_windows.py; line 769 - ",  value, self.size)

        return value

    @property
    def size(self):
        return len(self.buffer)
