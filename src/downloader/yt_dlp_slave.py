# Copyright (C) 2019-2021 Unrud <unrud@outlook.com>
#
# This file is part of Video Downloader.
#
# Video Downloader is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Video Downloader is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Video Downloader.  If not, see <http://www.gnu.org/licenses/>.

import contextlib
import glob
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.postprocessor.ffmpeg import (FFmpegPostProcessor,
                                         FFmpegPostProcessorError)
from yt_dlp.utils import dfxp2srt, sanitize_filename

# File names are typically limited to 255 bytes
MAX_OUTPUT_TITLE_LENGTH = 200
MAX_THUMBNAIL_RESOLUTION = 1024


def _short_filename(name, length):
    for i in range(len(name), -1, -1):
        output = name[:i].strip()
        if i < len(name):
            output += '…'
        output = sanitize_filename(output)
        # Check length with file system encoding
        if (len(output.encode(sys.getfilesystemencoding(), 'ignore'))
                < length):
            return output
    raise ValueError('can\'t shorten filename %r to %r bytes' % (name, length))


def _convert_filepath(info, files_to_delete, filepath, new_ext, type_='conv'):
    prefix = '.%s.%s' % (type_, new_ext)
    files_to_delete.append(filepath)
    info['__files_to_move'][filepath + prefix] = (
        info['__files_to_move'][filepath] + prefix)
    return filepath + prefix


class SubtitlesConverterPP(FFmpegPostProcessor):
    """A more robust subtitles converter"""

    def run(self, info):
        files_to_delete = []
        new_subtitles = {}
        for lang, sub in (info.get('requested_subtitles') or {}).items():
            filepath = sub.get('filepath')
            if not filepath:
                continue
            print('[yt_dlp_slave] Converting subtitle (%r, %r)' %
                  (lang, sub['ext']), file=sys.stderr, flush=True)
            if sub['ext'] in ['dfxp', 'ttml', 'tt']:
                # Try to use yt-dlp's internal dfxp2srt converter
                with open(filepath, 'rb') as f:
                    data = f.read()
                try:
                    data = dfxp2srt(data)
                except Exception:
                    files_to_delete.append(filepath)
                    traceback.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                    continue
                filepath = _convert_filepath(info, files_to_delete, filepath,
                                             'srt')
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(data)
            # Try to convert subtitles with ffmpeg
            new_filepath = _convert_filepath(info, files_to_delete, filepath,
                                             'vtt')
            try:
                self.run_ffmpeg(filepath, new_filepath, ['-f', 'webvtt'])
            except FFmpegPostProcessorError:
                files_to_delete.append(new_filepath)
                continue
            filepath = new_filepath
            # Fix broken WEBVTT files generated by FFmpeg (v4.4)
            # All leading spaces from the first line after a timestamp are
            # removed. If the first line only contains spaces it leaves an
            # empty line.
            with open(filepath, encoding='utf-8') as f:
                webvtt = f.read()
            new_webvtt = re.sub(r'(?<=\n\n)'  # check for empty line behind
                                r'([0-9.:]+ --> [0-9.:]+\n)'  # timestamp
                                r'\n'  # broken empty line
                                r'(?: +\n)* *',  # leading whitespaces
                                r'\1', webvtt)
            if webvtt != new_webvtt:
                filepath = _convert_filepath(info, files_to_delete, filepath,
                                             'vtt', type_='fix')
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_webvtt)
            new_subtitles[lang] = {**sub, 'filepath': filepath, 'ext': 'vtt'}
        info['requested_subtitles'] = new_subtitles
        return files_to_delete, info


class ThumbnailConverterPP(FFmpegPostProcessor):
    """Convert thumbnail to JPEG and if required decrease resolution"""

    def __init__(self, thumbnail_callback=None):
        super().__init__()
        self._thumbnail_callback = thumbnail_callback

    def run(self, info):
        files_to_delete = []
        new_thumbnails = []
        # Thumbnails are ordered worst to best
        for thumb in reversed(info.get('thumbnails') or []):
            filepath = thumb.get('filepath')
            if not filepath:
                continue
            if new_thumbnails:  # Convert only one thumbnail
                files_to_delete.append(filepath)
                continue
            print('[yt_dlp_slave] Converting thumbnail',
                  file=sys.stderr, flush=True)
            # Try to convert thumbnail with ffmpeg
            new_filepath = _convert_filepath(info, files_to_delete, filepath,
                                             'jpg')
            try:
                # FFmpeg uses % pattern for image input and output files
                self.real_run_ffmpeg(
                    # Disable pattern matching for input file
                    [(filepath, ['-f', 'image2', '-pattern_type', 'none'])],
                    # Escape % for output file
                    [(new_filepath.replace('%', '%%'), [
                        '-vf', ('scale=\'min({0},iw):min({0},ih):'
                                'force_original_aspect_ratio=decrease\''
                                ).format(MAX_THUMBNAIL_RESOLUTION)])])
            except FFmpegPostProcessorError:
                files_to_delete.append(new_filepath)
                continue
            filepath = new_filepath
            new_thumbnails.insert(0, {**thumb, 'filepath': filepath})
            if self._thumbnail_callback is not None:
                self._thumbnail_callback(os.path.abspath(filepath))
        info['thumbnails'] = new_thumbnails
        return files_to_delete, info


class RetryException(BaseException):
    pass


class YoutubeDLSlave:
    def _on_progress(self, d):
        if d['status'] not in ['downloading', 'finished']:
            return
        filename = d['filename']
        bytes_ = d.get('downloaded_bytes')
        if bytes_ is None:
            bytes_ = -1
        bytes_total = d.get('total_bytes')
        if bytes_total is None:
            bytes_total = d.get('total_bytes_estimate')
        if bytes_total is None:
            bytes_total = -1
        if d['status'] == 'downloading':
            fragments = d.get('fragment_index')
            if fragments is None:
                fragments = -1
            fragments_total = d.get('fragment_count')
            if fragments_total is None:
                fragments_total = -1
            if bytes_ >= 0 and bytes_total >= 0:
                progress = bytes_ / bytes_total if bytes_total > 0 else -1
            elif fragments >= 0 and fragments_total >= 0:
                progress = (fragments / fragments_total
                            if fragments_total > 0 else -1)
            else:
                progress = -1
            eta = d.get('eta')
            if eta is None:
                eta = -1
            speed = d.get('speed')
            if speed is None:
                speed = -1
            speed = round(speed)
        elif d['status'] == 'finished':
            progress = -1
            eta = -1
            speed = -1
        self._handler.on_progress(filename, progress, bytes_, bytes_total, eta,
                                  speed)

    def _load_playlist(self, url):
        '''Retrieve info for all videos available on URL.

        Returns the absolute paths of the generated and downloaded files:
        ([info_dict, ...], skipped videos)
        '''
        with tempfile.TemporaryDirectory() as temp_dir:
            os.chdir(temp_dir)
            while True:
                ydl_opts = {**self.ydl_opts,
                            'writeinfojson': True,
                            'skip_download': True,
                            'outtmpl': '%(autonumber)s.%(ext)s'}
                saved_skipped_count = self._skipped_count
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        for args in self.extra_postprocessors:
                            ydl.add_post_processor(*args)
                        ydl.download([url])
                except RetryException:
                    continue
                break
            info_dicts = []
            for filename in sorted(os.listdir(temp_dir)):
                if re.fullmatch(r'[0-9]+\.info\.json', filename):
                    with open(os.path.join(temp_dir, filename),
                              encoding='utf-8') as f:
                        info_dicts.append(json.load(f))
        return info_dicts, self._skipped_count - saved_skipped_count

    def _load_video(self, dir_, info_path):
        class GetFilepathPP(PostProcessor):
            def run(self, info):
                nonlocal filepath
                filepath = info['filepath']
                return [], info
        filepath = None
        os.chdir(dir_)
        while True:
            try:
                with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                    for args in self.extra_postprocessors:
                        ydl.add_post_processor(*args)
                    ydl.add_post_processor(GetFilepathPP())
                    ydl.download_with_info_file(info_path)
            except RetryException:
                continue
            break
        return os.path.abspath(filepath)

    def debug(self, msg):
        print(msg, file=sys.stderr, flush=True)

    def warning(self, msg):
        print(msg, file=sys.stderr, flush=True)

    def error(self, msg):
        print(msg, file=sys.stderr, flush=True)
        # Handle authentication requests
        if (self._allow_authentication_request and
                re.search(r'\b[Ss]ign in\b|--username', msg)):
            if self._skip_authentication:
                self._skipped_count += 1
                return
            user, password = self._handler.on_login_request()
            if not user and not password:
                self._skip_authentication = True
                self._skipped_count += 1
                return
            self.ydl_opts['username'] = user
            self.ydl_opts['password'] = password
            self._allow_authentication_request = False
            raise RetryException(msg)
        if self._allow_authentication_request and '--video-password' in msg:
            if self._skip_authentication:
                self._skipped_count += 1
                return
            password = self._handler.on_password_request()
            if not password:
                self._skip_authentication = True
                self._skipped_count += 1
                return
            self.ydl_opts['videopassword'] = password
            self._allow_authentication_request = False
            raise RetryException(msg)
        # Ignore missing xattr support
        if 'This filesystem doesn\'t support extended attributes.' in msg:
            return
        self._handler.on_error(msg)
        sys.exit(1)

    @staticmethod
    def _find_existing_download(download_dir, output_title, mode):
        for filepath in glob.iglob(glob.escape(
                os.path.join(download_dir, output_title)) + '.*'):
            filename = os.path.basename(filepath)
            file_title, file_ext = os.path.splitext(filename)
            if file_title == output_title and (
                    mode == 'audio' and file_ext.lower() == '.mp3' or
                    mode != 'audio' and file_ext.lower() != '.mp3') and (
                    os.path.isfile(filepath)):
                return filename
        return None

    def __init__(self, handler):
        self._handler = handler
        self._allow_authentication_request = True
        self._skip_authentication = False
        self._skipped_count = 0
        self.ydl_opts = {
            'logger': self,
            'logtostderr': True,
            'no_color': True,
            'progress_hooks': [self._on_progress],
            'fixup': 'detect_or_warn',
            'ignoreerrors': True,  # handled via logger error callback
            'retries': 10,
            'fragment_retries': 10,
            'subtitleslangs': ['all'],
            'subtitlesformat': 'vtt/best',
            'keepvideo': True,
            'allow_playlist_files': False,  # no info.json files for playlists
            # Include id and format_id in outtmpl to prevent yt-dlp
            # from continuing wrong file
            'outtmpl': '%(id)s.%(format_id)s.%(ext)s',
            'postprocessors': [
                {'key': 'FFmpegMetadata'},
                {'key': 'FFmpegEmbedSubtitle'},
                {'key': 'XAttrMetadata'}]}
        self.extra_postprocessors = [
            (ThumbnailConverterPP(self._handler.on_download_thumbnail),
             'before_dl'),
            (SubtitlesConverterPP(), 'before_dl')]
        mode = self._handler.get_mode()
        if mode == 'audio':
            self.ydl_opts['format'] = 'bestaudio/best'
            self.ydl_opts['postprocessors'].insert(0, {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192'})
            self.ydl_opts['postprocessors'].insert(1, {
                'key': 'EmbedThumbnail',
                'already_have_thumbnail': True})
        else:
            self.ydl_opts['format_sort'] = [
                'res~%d' % self._handler.get_resolution()]
            if self._handler.get_prefer_mpeg():
                self.ydl_opts['format_sort'].append('+codec:avc:m4a')
        url = self._handler.get_url()
        download_dir = os.path.abspath(self._handler.get_download_dir())
        requested_automatic_subtitles = set(
            self._handler.get_automatic_subtitles())
        with tempfile.TemporaryDirectory() as temp_dir:
            self.ydl_opts['cookiefile'] = os.path.join(temp_dir, 'cookies')
            self.ydl_opts['playlistend'] = 2
            # Test playlist
            info_testplaylist, skipped_testplaylist = self._load_playlist(url)
            self.ydl_opts['noplaylist'] = True
            if len(info_testplaylist) + skipped_testplaylist > 1:
                info_noplaylist, skipped_noplaylist = self._load_playlist(url)
            else:
                info_noplaylist = info_testplaylist
                skipped_noplaylist = skipped_testplaylist
            del self.ydl_opts['noplaylist']
            del self.ydl_opts['playlistend']
            if (len(info_testplaylist) + skipped_testplaylist >
                    len(info_noplaylist) + skipped_noplaylist):
                self.ydl_opts['noplaylist'] = (
                    not self._handler.on_playlist_request())
                if not self.ydl_opts['noplaylist']:
                    info_playlist, _ = self._load_playlist(url)
                else:
                    info_playlist = info_noplaylist
            elif len(info_testplaylist) + skipped_testplaylist > 1:
                info_playlist, _ = self._load_playlist(url)
            else:
                info_playlist = info_testplaylist
            # Download videos
            self._allow_authentication_request = False
            self.ydl_opts['writesubtitles'] = True
            self.ydl_opts['writeautomaticsub'] = True
            self.ydl_opts['writethumbnail'] = True
            for i, info in enumerate(info_playlist):
                title = info.get('title') or info.get('id') or 'video'
                output_title = _short_filename(title, MAX_OUTPUT_TITLE_LENGTH)
                self._handler.on_download_start(i, len(info_playlist), title)
                # Lock download name to prevent other instances from
                # writing to the same files
                while not self._handler.on_download_lock(output_title):
                    time.sleep(1)
                automatic_captions = info.get('automatic_captions') or {}
                skip_captions = {*(info.get('subtitles') or {})}
                new_automatic_captions = {}
                for lang, subs in automatic_captions.items():
                    if lang in skip_captions:
                        continue
                    for requested_lang in requested_automatic_subtitles:
                        if requested_lang == 'all' or requested_lang == lang:
                            break
                        # Translated subtitles
                        if (lang.startswith(requested_lang+'-')
                                and requested_lang not in skip_captions
                                and requested_lang not in automatic_captions):
                            skip_captions.add(requested_lang)
                            break
                    else:
                        continue
                    new_automatic_captions[lang] = subs
                if automatic_captions != new_automatic_captions:
                    info['_backup_automatic_captions'] = automatic_captions
                    info['automatic_captions'] = new_automatic_captions
                # Check if we already got the file
                existing_filename = self._find_existing_download(
                    download_dir, output_title, mode)
                if existing_filename is not None:
                    self._handler.on_download_finished(existing_filename)
                    continue
                # Download into separate directory because yt-dlp generates
                # many temporary files
                temp_download_dir = os.path.join(
                    download_dir, output_title + '.part')
                try:
                    os.makedirs(download_dir, exist_ok=True)
                    os.makedirs(temp_download_dir, exist_ok=True)
                except OSError as e:
                    traceback.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                    self._handler.on_error(
                        'ERROR: Failed to create download folder: %s' % e)
                    sys.exit(1)
                info_path = os.path.join(
                    temp_download_dir,
                    sanitize_filename((info.get('id') or '') + '.info.json'))
                with open(info_path, 'w', encoding='utf-8') as f:
                    json.dump(info, f)
                temp_filepath = self._load_video(temp_download_dir, info_path)
                _, filename_ext = os.path.splitext(temp_filepath)
                filename = output_title + filename_ext
                # Move finished download from download to target dir
                try:
                    os.replace(temp_filepath,
                               os.path.join(download_dir, filename))
                except OSError as e:
                    traceback.print_exc(file=sys.stderr)
                    sys.stderr.flush()
                    self._handler.on_error((
                        'ERROR: Falied to move finished download to '
                        'download folder: %s') % e)
                    sys.exit(1)
                # Delete download directory
                with contextlib.suppress(OSError):
                    shutil.rmtree(temp_download_dir)
                self._handler.on_download_finished(filename)
