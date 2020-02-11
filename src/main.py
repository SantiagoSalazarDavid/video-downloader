# main.py
#
# Copyright 2019 Unrud <unrud@outlook.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import gettext
import sys
import gi
from gi.repository import GLib, Gtk, Gio

from video_downloader.authentication_dialog import LoginDialog, PasswordDialog
from video_downloader.model import Handler, Model
from video_downloader.window import Window
from video_downloader.playlist_dialog import PlaylistDialog
from video_downloader.util import bind_property

N_ = gettext.gettext


class Application(Gtk.Application, Handler):
    def __init__(self):
        super().__init__(application_id='com.github.unrud.VideoDownloader',
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        GLib.set_application_name(N_('Video Downloader'))
        self.model = Model(self)
        self._settings = Gio.Settings.new(self.props.application_id)
        self.model.download_dir = self._settings.get_string('download-folder')
        self.model.mode = self._settings.get_string('mode')
        r = self._settings.get_uint('resolution')
        for resolution in sorted(x[0] for x in self.model.resolutions):
            if r <= resolution:
                break
        self.model.resolution = resolution
        bind_property(self.model, 'mode', func_a_to_b=lambda x:
                      self._settings.set_string('mode', x))
        bind_property(self.model, 'resolution', func_a_to_b=lambda x:
                      self._settings.set_uint('resolution', x))

    def do_activate(self):
        for name in self.model.actions.list_actions():
            self.add_action(self.model.actions.lookup_action(name))
        win = self.props.active_window
        if not win:
            win = Window(application=self)
            win.set_default_icon_name(self.props.application_id)
        win.present()

    def on_playlist_request(self):
        dialog = PlaylistDialog(self.props.active_window)
        res = dialog.run()
        dialog.destroy()
        return res == Gtk.ResponseType.YES

    def on_login_request(self):
        dialog = LoginDialog(self.props.active_window)
        res = dialog.run()
        dialog.destroy()
        if res == Gtk.ResponseType.OK:
            return (dialog.username, dialog.password)
        return ('', '')

    def on_videopassword_request(self) -> str:
        dialog = PasswordDialog(self.props.active_window)
        res = dialog.run()
        dialog.destroy()
        if res == Gtk.ResponseType.OK:
            return dialog.password
        return ''


def main(version):
    app = Application()
    return app.run(sys.argv)
