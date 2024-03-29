# Copyright 2012 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Handlers that are not directly related to course content."""

__author__ = 'Saifu Angto (saifu@google.com)'

import collections
import datetime
import HTMLParser
import logging
import os
import re
import urllib
import urlparse

import jinja2
import sites
import webapp2
from webob import multidict

import appengine_config
from common import jinja_utils
from common import locales
from common import resource
from common import safe_dom
from common import schema_fields
from common import tags
from common import users
from common import utils as common_utils
from common.crypto import XsrfTokenManager
from models import courses
from models import custom_modules
from models import resources_display
from models import models
from models import transforms
from models.config import ConfigProperty
from models.courses import Course
from models.models import Student
from models.models import StudentProfileDAO
from models.models import TransientStudent
from models.roles import Roles

# The name of the template dict key that stores a course's base location.
COURSE_BASE_KEY = 'gcb_course_base'

# The name of the template dict key that stores data from course.yaml.
COURSE_INFO_KEY = 'course_info'

# The name of the cookie used to store the locale prefs for users out of session
GUEST_LOCALE_COOKIE = 'cb-user-locale'
GUEST_LOCALE_COOKIE_MAX_AGE_SEC = 48 * 60 * 60  # 48 hours

TRANSIENT_STUDENT = TransientStudent()

# Whether to output debug info into the page.
CAN_PUT_DEBUG_INFO_INTO_PAGES = ConfigProperty(
    'gcb_can_put_debug_info_into_pages', bool, (
        'Whether or not to put debugging information into the web pages. '
        'This may be useful for debugging purposes if you develop custom '
        'Course Builder features or extensions.'), False,
    label='Debug information')

# Whether to record page load/unload events in a database.
CAN_PERSIST_PAGE_EVENTS = ConfigProperty(
    'gcb_can_persist_page_events', bool, (
        'Whether or not to record student page interactions in a '
        'datastore. Without event recording, you cannot analyze student '
        'page interactions. On the other hand, no event recording reduces '
        'the number of datastore operations and minimizes the use of Google '
        'App Engine quota. Turn event recording on if you want to analyze '
        'this data.'),
    False, label='Record page events')


# Whether to record tag events in a database.
CAN_PERSIST_TAG_EVENTS = ConfigProperty(
    'gcb_can_persist_tag_events', bool, (
        'Whether or not to record student tag interactions in a '
        'datastore. Without event recording, you cannot analyze student '
        'tag interactions. On the other hand, no event recording reduces '
        'the number of datastore operations and minimizes the use of Google '
        'App Engine quota. Turn event recording on if you want to analyze '
        'this data.'),
    False, label='Record tag events')


# Whether to record events in a database.
CAN_PERSIST_ACTIVITY_EVENTS = ConfigProperty(
    'gcb_can_persist_activity_events', bool, (
        'Whether or not to record student activity interactions in a '
        'datastore. Without event recording, you cannot analyze student '
        'activity interactions. On the other hand, no event recording reduces '
        'the number of datastore operations and minimizes the use of Google '
        'App Engine quota. Turn event recording on if you want to analyze '
        'this data.'),
    False, label='Record activities')


# Date format string for displaying datetimes in UTC.
# Example: 2013-03-21 13:00 UTC
HUMAN_READABLE_DATETIME_FORMAT = '%Y-%m-%d, %H:%M UTC'

# Date format string for displaying dates. Example: 2013-03-21
HUMAN_READABLE_DATE_FORMAT = '%Y-%m-%d'

# Time format string for displaying times. Example: 01:16:40 UTC.
HUMAN_READABLE_TIME_FORMAT = '%H:%M:%S UTC'


class RESTHandlerMixin(object):
    """A mixin class to mark any handler as REST handler."""
    pass


class StarRouteHandlerMixin(object):
    """A mixin class to mark any handler that supports '*' routes."""
    pass


class PageInitializer(object):
    """Abstract class that defines an interface to initialize page headers."""

    @classmethod
    def initialize(cls, template_value):
        raise NotImplementedError


class DefaultPageInitializer(PageInitializer):
    """Implements default page initializer."""

    @classmethod
    def initialize(cls, template_value):
        pass


class PageInitializerService(object):
    """Installs the appropriate PageInitializer."""
    _page_initializer = DefaultPageInitializer

    @classmethod
    def get(cls):
        return cls._page_initializer

    @classmethod
    def set(cls, page_initializer):
        cls._page_initializer = page_initializer


class ReflectiveRequestHandler(object):
    """Uses reflection to handle custom get() and post() requests.

    Use this class as a mix-in with any webapp2.RequestHandler to allow request
    dispatching to multiple get() and post() methods based on the 'action'
    parameter.

    Open your existing webapp2.RequestHandler, add this class as a mix-in.
    Define the following class variables:

        default_action = 'list'
        get_actions = ['default_action', 'edit']
        post_actions = ['save']

    Add instance methods named get_list(self), get_edit(self), post_save(self).
    These methods will now be called automatically based on the 'action'
    GET/POST parameter.
    """

    def create_xsrf_token(self, action):
        return XsrfTokenManager.create_xsrf_token(action)

    def get(self):
        """Handles GET."""
        action = self.request.get('action')
        if not action:
            action = self.default_action

        if action not in self.get_actions:
            self.error(404)
            return

        handler = getattr(self, 'get_%s' % action)
        if not handler:
            self.error(404)
            return

        return handler()

    def post(self):
        """Handles POST."""
        action = self.request.get('action')
        if not action or action not in self.post_actions:
            self.error(404)
            return

        handler = getattr(self, 'post_%s' % action)
        if not handler:
            self.error(404)
            return

        # Each POST request must have valid XSRF token.
        xsrf_token = self.request.get('xsrf_token')
        if not XsrfTokenManager.is_xsrf_token_valid(xsrf_token, action):
            self.error(403)
            return

        return handler()


class HtmlHooks(object):

    # As of Q1, 2015, hook points moved from "where-ever in the course
    # settings is convenient" to "Anywhere under the top-level 'html_hooks'
    # item".  Older courses may have these items still referenced from the
    # root of the settings hierarchy, rather than under "html_hooks".  Rather
    # than modifying the course settings, we simply also look for these legacy
    # items in the old locations.
    BACKWARD_COMPATIBILITY_ITEMS = [
        'base.before_head_tag_ends',
        'base.after_body_tag_begins',
        'base.after_navbar_begins',
        'base.before_navbar_ends',
        'base.after_top_content_ends',
        'base.after_main_content_ends',
        'base.before_body_tag_ends',
        'unit.after_leftnav_begins',
        'unit.before_leftnav_ends',
        'unit.after_content_begins',
        'unit.before_content_ends',
        'preview.after_top_content_ends',
        'preview.after_main_content_ends',
        ]

    # We used to use colons to separate path components in hook names.  Now
    # that I18N is using colons to delimit key components, we need to pick
    # a different separator.  There may be old Jinja templates using the old
    # naming style, so continue to permit it.
    BACKWARD_COMPATIBILITY_SEPARATOR = ':'

    # Name for the top-level course settings section now holding the hooks,
    # all of the hooks, and nothing but the hooks.
    HTML_HOOKS = 'html_hooks'

    # Extension modules may be called back from HtmlHooks.__init__.  In
    # particular, I18N's mode of operation is to hook load functionality to
    # replace strings with translated versions.
    POST_LOAD_CALLBACKS = []

    # Path component separator.  Allows sub-structure within the html_hooks
    # top-level dict.
    SEPARATOR = '.'

    def __init__(self, course, prefs=None):
        if prefs is None:
            prefs = models.StudentPreferencesDAO.load_or_default()

        # Fetch all the hooks.  Since these are coming from the course
        # settings, getting them all is not too inefficient.
        self.content = self.get_all(course)

        # Call callbacks to let extension modules know we have text loaded,
        # in case they need to modify, replace, or extend anything.
        for callback in self.POST_LOAD_CALLBACKS:
            callback(self.content)

        # When the course admin sees hooks, we may need to add nonblank
        # text so the admin can have a place to click to edit them.
        self.show_admin_content = False
        if (prefs and prefs.show_hooks and
            Roles.is_course_admin(course.app_context)):
            self.show_admin_content = True
        if course.version == courses.CourseModel12.VERSION:
            self.show_admin_content = False
        if self.show_admin_content:
            self.update_for_admin()

    def update_for_admin(self):
        """Show HTML hooks with non-blank text if admin has edit pref set.

        If we are displaying to a course admin, and the admin has enabled
        a preference, we want to ensure that each HTML hook point has some
        non-blank text in it.  (Hooks often carry only scripts, or other
        non-displaying tags).  Having some actual text in the tag forces
        browsers to give it a visible component on the page.  Clicking on
        this component permits the admin to edit the item.
        """

        class VisibleHtmlParser(HTMLParser.HTMLParser):

            def __init__(self, *args, **kwargs):
                HTMLParser.HTMLParser.__init__(self, *args, **kwargs)
                self._has_visible_content = False

            def handle_starttag(self, unused_tag, unused_attrs):
                # Not 100% guaranteed; e.g., <p> does not guarantee content,
                # but <button> does -- even if the <button> does not contain
                # data/entity/char.  I don't want to spend a lot of logic
                # looking for specific cases, and this behavior is enough.
                self._has_visible_content = True

            def handle_data(self, data):
                if data.strip():
                    self._has_visible_content = True

            def handle_entityref(self, unused_data):
                self._has_visible_content = True

            def handle_charref(self, unused_data):
                self._has_visible_content = True

            def has_visible_content(self):
                return self._has_visible_content

            def reset(self):
                HTMLParser.HTMLParser.reset(self)
                self._has_visible_content = False

        parser = VisibleHtmlParser()

        for key, value in self.content.iteritems():
            parser.reset()
            parser.feed(value)
            parser.close()
            if not parser.has_visible_content():
                self.content[key] += key

    @classmethod
    def _get_content_from(cls, name, environ):
        # Look up desired content chunk in course.yaml dict/sub-dict.
        content = None
        for part in name.split(cls.SEPARATOR):
            if part in environ:
                item = environ[part]
                if isinstance(item, basestring):
                    content = item
                else:
                    environ = item
        return content

    @classmethod
    def get_content(cls, course, name):
        environ = course.app_context.get_environ()

        # Prefer getting hook content from html_hooks sub-dict within
        # course settings.
        content = cls._get_content_from(name, environ.get(cls.HTML_HOOKS, {}))

        # For backward compatibility, fall back to looking in top level.
        if content is None:
            content = cls._get_content_from(name, environ)
        return content

    @classmethod
    def get_all(cls, course):
        """Get all hook names and associated content."""
        ret = {}
        # Look through the backward-compatibility items.  These may not all
        # exist, but pick up whatever does already exist.
        environ = course.app_context.get_environ()
        for backward_compatibility_item in cls.BACKWARD_COMPATIBILITY_ITEMS:
            value = cls._get_content_from(backward_compatibility_item, environ)
            if value:
                ret[backward_compatibility_item] = value

        # Pick up hook values from the official location under 'html_hooks'
        # within course settings.  These can override backward-compatible
        # versions when both are present.
        def find_leaves(environ, parent_names, ret):
            for name, value in environ.iteritems():
                if isinstance(value, basestring):
                    full_name = cls.SEPARATOR.join(parent_names + [name])
                    ret[full_name] = value
                elif isinstance(value, dict):
                    find_leaves(value, parent_names + [name], ret)

        find_leaves(environ[cls.HTML_HOOKS], [], ret)
        return ret


    def insert(self, name):
        name = name.replace(self.BACKWARD_COMPATIBILITY_SEPARATOR,
                            self.SEPARATOR)
        content = self.content.get(name, '')

        # Add the content to the page in response to the hook call.
        hook_div = safe_dom.Element('div', className='gcb-html-hook',
                                    id=re.sub('[^a-zA-Z-]', '-', name))
        hook_div.add_child(tags.html_to_safe_dom(content, self))

        # Mark up content to enable edit controls
        if self.show_admin_content:
            hook_div.add_attribute(onclick='gcb_edit_hook_point("%s")' % name)
            hook_div.add_attribute(className='gcb-html-hook-edit')
        return jinja2.Markup(hook_div.sanitized)


class ResourceHtmlHook(resource.AbstractResourceHandler):
    """Provide a class to allow treating this resource type polymorphically."""

    TYPE = 'html_hook'
    NAME = 'name'
    CONTENT = 'content'

    @classmethod
    def get_resource(cls, course, key):
        return cls.get_data_dict(course, key)

    @classmethod
    def get_resource_title(cls, rsrc):
        return rsrc[cls.NAME]

    @classmethod
    def get_schema(cls, unused_course, unused_key):
        ret = schema_fields.FieldRegistry(
            'HTML Hooks',
            description='HTML fragments that can be inserted at arbitrary '
            'points in student-visible pages using the syntax: '
            ' {{ html_hooks.insert(\'name_of_hook_section\') }} ')
        ret.add_property(schema_fields.SchemaField(
            cls.NAME, 'Name', 'string', i18n=False))
        ret.add_property(schema_fields.SchemaField(
            cls.CONTENT, 'Content', 'html', editable=True,
            description='HTML content injected into page where hook '
            'is referenced.'))
        return ret

    @classmethod
    def to_data_dict(cls, key, content):
        return {
            cls.NAME: key,
            cls.CONTENT: content,
        }

    @classmethod
    def get_data_dict(cls, course, key):
        return cls.to_data_dict(key, HtmlHooks.get_content(course, key))

    @classmethod
    def get_view_url(cls, rsrc):
        return None

    @classmethod
    def get_edit_url(cls, key):
        return 'dashboard?%s' % urllib.urlencode({
            'action': 'edit_html_hook',
            'key': key
            })

    @classmethod
    def get_all(cls, course):
        """Returns key/value pairs of resource.Key -> <html-hook resource>"""

        ret = {}
        for name, content in HtmlHooks.get_all(course).iteritems():
            key = resource.Key(cls.TYPE, name, course)
            value = {
                cls.NAME: name,
                cls.CONTENT: content
                }
            ret[key] = value
        return ret


class CronHandler(webapp2.RequestHandler):
    """Cron HTTP handlers should ensure caller is AppEngine, not external."""

    def is_not_from_appengine_cron(self):
        if 'X-AppEngine-Cron' not in self.request.headers:
            self.response.out.write('Forbidden.')
            self.response.set_status(403)
            return True
        return False


class AbstractAllCoursesCronHandler(CronHandler):
    """Common logic enabling Cron handlers to operate on all courses.

    Individual cron handlers commonly need to operate against all courses
    in an installation.  This class provides common base functionality
    to do the iteration over courses and error handling, freeing
    derived classes to implement only the feature-specific business logic.

    Use by extending is_globally_enabled(), is_enabled_for_course() and
    putting the business logic in cron_action().
    """

    @classmethod
    def is_globally_enabled(cls):
        """Derived classes tell base class whether feature is enabled."""
        raise NotImplementedError()

    @classmethod
    def is_enabled_for_course(cls, app_context):
        """Derived classes tell whether feature is enabled for a course."""
        raise NotImplementedError()

    def global_setup(self):
        """Perform any expensive work.  Return value passed to cron_action()."""
        return None

    def cron_action(self, app_context, global_state):
        """Do work for courses where is_enabled_for_course() returned true."""
        raise NotImplementedError()

    def get(self):
        if self.is_not_from_appengine_cron():
            return
        self._internal_get()

    @classmethod
    def _for_testing_only_get(cls):
        """Permits direct call to code under test, as opposed to using HTTP."""
        response = webapp2.Response()
        instance = cls()
        instance.response = response
        instance._internal_get()  # pylint: disable=protected-access

    def _internal_get(self):
        """Separate function from get() to permit simple calling by tests."""

        if self.is_globally_enabled():
            global_state = self.global_setup()
            for app_context in sites.get_all_courses():
                if self.is_enabled_for_course(app_context):
                    namespace = app_context.get_namespace_name()
                    with common_utils.Namespace(namespace):
                        try:
                            self.cron_action(app_context, global_state)
                        except Exception, ex:  # pylint: disable=broad-except
                            logging.critical(
                                'Cron handler %s for course %s: %s',
                                self.__class__.__name__, app_context.get_slug(),
                                str(ex))
                            common_utils.log_exception_origin()
                else:
                    logging.info(
                        'Skipping cron handler %s for course %s',
                        self.__class__.__name__, app_context.get_slug())
            self.response.write('OK.')
        else:
            logging.info('Skipping cron handler %s; globally disabled.',
                         self.__class__.__name__)
            self.response.write('Disabled.')
        self.response.set_status(200)


class ApplicationHandler(webapp2.RequestHandler):
    """A handler that is aware of the application context."""

    LEFT_LINKS = []
    RIGHT_LINKS = []
    EXTRA_GLOBAL_CSS_URLS = []
    EXTRA_GLOBAL_JS_URLS = []

    @classmethod
    def is_absolute(cls, url):
        return sites.ApplicationContext.is_absolute_url(url)

    @classmethod
    def get_base_href(cls, handler):
        """Computes current course <base> href."""
        base = handler.app_context.get_slug()
        if not base.endswith('/'):
            base = '%s/' % base

        # For IE to work with the <base> tag, its href must be an absolute URL.
        if not sites.ApplicationContext.is_absolute_url(base):
            parts = urlparse.urlparse(handler.request.url)
            base = urlparse.urlunparse(
                (parts.scheme, parts.netloc, base, None, None, None))
        return base

    def render_template_to_html(self, template_values, template_file,
                                additional_dirs=None):
        courses.Course.set_current(self.get_course())
        models.MemcacheManager.begin_readonly()
        try:
            template = self.get_template(template_file, additional_dirs)
            return jinja2.utils.Markup(
                template.render(template_values, autoescape=True))
        finally:
            models.MemcacheManager.end_readonly()
            courses.Course.clear_current()

    def get_template(self, template_file, additional_dirs=None, prefs=None):
        raise NotImplementedError()

    @classmethod
    def canonicalize_url_for(cls, app_context, location):
        """Adds the current namespace URL prefix to the relative 'location'."""
        return app_context.canonicalize_url(location)

    def canonicalize_url(self, location):
        if hasattr(self, 'app_context'):
            return self.app_context.canonicalize_url(location)
        else:
            return location

    def redirect(self, location, normalize=True):
        if normalize:
            location = self.canonicalize_url(location)
        super(ApplicationHandler, self).redirect(location)


class CourseHandler(ApplicationHandler):
    """Base handler that is aware of the current course."""

    FOOTER_ITEMS = []

    def __init__(self, *args, **kwargs):
        super(CourseHandler, self).__init__(*args, **kwargs)
        self.course = None
        self.template_value = {}

    def get_user(self):
        """Get the current user."""
        return users.get_current_user()

    def get_student(self):
        """Get the current student."""
        user = self.get_user()
        if user is None:
            return None
        return Student.get_by_user(user)

    def _pick_first_valid_locale_from_list(self, desired_locales):
        available_locales = self.app_context.get_allowed_locales()
        for lang in desired_locales:
            for available_locale in available_locales:
                if lang.lower() == available_locale.lower():
                    return lang
        return None

    def get_locale_for(self, request, app_context, student=None, prefs=None):
        """Returns a locale that should be used by this request."""
        hl = request.get('hl')
        if hl and hl in self.app_context.get_allowed_locales():
            return hl

        if self.get_user():
            # check if student has any locale labels assigned
            if student is None:
                student = self.get_student()
            if student and student.is_enrolled and not student.is_transient:
                student_label_ids = student.get_labels_of_type(
                    models.LabelDTO.LABEL_TYPE_LOCALE)
                if student_label_ids:
                    all_labels = models.LabelDAO.get_all_of_type(
                        models.LabelDTO.LABEL_TYPE_LOCALE)
                    student_locales = []
                    for label in all_labels:
                        if label.type != models.LabelDTO.LABEL_TYPE_LOCALE:
                            continue
                        if label.id in student_label_ids:
                            student_locales.append(label.title)
                    locale = self._pick_first_valid_locale_from_list(
                        student_locales)
                    if locale:
                        return locale

            # check if user preferences have been set
            if prefs is None:
                prefs = models.StudentPreferencesDAO.load_or_default()
            if prefs is not None and prefs.locale is not None:
                return prefs.locale

        locale_cookie = self.request.cookies.get(GUEST_LOCALE_COOKIE)
        if locale_cookie and (
                locale_cookie in self.app_context.get_allowed_locales()):
            return locale_cookie

        # check if accept language has been set
        accept_langs = request.headers.get('Accept-Language')
        locale = self._pick_first_valid_locale_from_list(
            [lang for lang, _ in locales.parse_accept_language(accept_langs)])
        if locale:
            return locale

        return app_context.default_locale

    def gettext(self, text):
        old_locale = self.app_context.get_current_locale()
        try:
            new_locale = self.get_locale_for(self.request, self.app_context)
            self.app_context.set_current_locale(new_locale)
            return self.app_context.gettext(text)
        finally:
            self.app_context.set_current_locale(old_locale)

    def get_course(self):
        """Get current course."""
        if not self.course:
            self.course = Course(self)
        return self.course

    def get_track_matching_student(self, student):
        """Gets units whose labels match those on the student."""
        return self.get_course().get_track_matching_student(student)

    def get_progress_tracker(self):
        """Gets the progress tracker for the course."""
        return self.get_course().get_progress_tracker()

    def find_unit_by_id(self, unit_id):
        """Gets a unit with a specific id or fails with an exception."""
        return self.get_course().find_unit_by_id(unit_id)

    def get_units(self):
        """Gets all units in the course."""
        return self.get_course().get_units()

    def get_lessons(self, unit_id):
        """Gets all lessons (in order) in the specific course unit."""
        return self.get_course().get_lessons(unit_id)

    @classmethod
    def _cache_debug_info(cls, cache):
        items = []
        for key, entry in cache.items.iteritems():
            updated_on = None
            if entry:
                updated_on = entry.updated_on()
            items.append('entry: %s, %s' % (key, updated_on))
        return items

    @classmethod
    def debug_info(cls):
        """Generates a debug info for this request."""

        # we only want to run import if this method is called; most of the
        # it is not; we also have circular import dependencies if we were to
        # put them at the top...
        from models import vfs
        from modules.i18n_dashboard import i18n_dashboard
        vfs_items = cls._cache_debug_info(
            vfs.ProcessScopedVfsCache.instance().cache)
        rb_items = cls._cache_debug_info(
            i18n_dashboard.ProcessScopedResourceBundleCache.instance().cache)
        return ''.join([
              '\nDebug Info: %s' % datetime.datetime.utcnow(),
              '\n\nServer Environment Variables: %s' % '\n'.join([
                  'item: %s, %s' % (key, value)
                  for key, value in os.environ.iteritems()]),
              '\n\nVfsCacheKeys:\n%s' % '\n'.join(vfs_items),
              '\n\nResourceBundlesCache:\n%s' % '\n'.join(rb_items),
              ])

    def init_template_values(self, environ, prefs=None):
        """Initializes template variables with common values."""
        self.template_value[COURSE_INFO_KEY] = environ
        self.template_value[
            'page_locale'] = self.app_context.get_current_locale()
        self.template_value['html_hooks'] = HtmlHooks(
            self.get_course(), prefs=prefs)
        self.template_value['is_course_admin'] = Roles.is_course_admin(
            self.app_context)
        self.template_value['can_see_drafts'] = (
            custom_modules.can_see_drafts(self.app_context))
        self.template_value[
            'is_read_write_course'] = self.app_context.fs.is_read_write()
        self.template_value['is_super_admin'] = Roles.is_super_admin()
        self.template_value[COURSE_BASE_KEY] = self.get_base_href(self)
        self.template_value['left_links'] = []
        for func in self.LEFT_LINKS:
            self.template_value['left_links'].extend(func(self.app_context))
        self.template_value['right_links'] = []
        for func in self.RIGHT_LINKS:
            self.template_value['right_links'].extend(func(self.app_context))
        self.template_value['footer_items'] = []
        for func in self.FOOTER_ITEMS:
            self.template_value['footer_items'].extend(func(self))

        if not prefs:
            prefs = models.StudentPreferencesDAO.load_or_default()
        self.template_value['student_preferences'] = prefs

        if (Roles.is_course_admin(self.app_context) and
            not appengine_config.PRODUCTION_MODE and
            prefs and prefs.show_jinja_context):

            @jinja2.contextfunction
            def get_context(context):
                return context
            self.template_value['context'] = get_context

        if CAN_PUT_DEBUG_INFO_INTO_PAGES.value:
            self.template_value['debug_info'] = self.debug_info()

        self.template_value[
            'extra_global_css_urls'] = self.EXTRA_GLOBAL_CSS_URLS
        self.template_value[
            'extra_global_js_urls'] = self.EXTRA_GLOBAL_JS_URLS

        # Common template information for the locale picker (only shown for
        # user in session)
        can_student_change_locale = (
            self.get_course().get_course_setting('can_student_change_locale')
            or self.get_course().app_context.can_pick_all_locales())
        if can_student_change_locale:
            self.template_value['available_locales'] = [
                {
                    'name': locales.get_locale_display_name(loc),
                    'value': loc
                } for loc in self.app_context.get_allowed_locales()]
            self.template_value['locale_xsrf_token'] = (
                XsrfTokenManager.create_xsrf_token(
                    StudentLocaleRESTHandler.XSRF_TOKEN_NAME))
            self.template_value['selected_locale'] = self.get_locale_for(
                self.request, self.app_context, prefs=prefs)

    def get_template(self, template_file, additional_dirs=None, prefs=None):
        """Computes location of template files for the current namespace."""

        _p = self.app_context.get_environ()
        self.init_template_values(_p, prefs=prefs)
        template_environ = self.app_context.get_template_environ(
            self.app_context.get_current_locale(), additional_dirs)
        template_environ.filters[
            'gcb_tags'] = jinja_utils.get_gcb_tags_filter(self)
        template_environ.globals.update({
            'display_unit_title': (
                lambda unit: resources_display.display_unit_title(
                    unit, self.app_context)),
            'display_short_unit_title': (
                lambda unit: resources_display.display_short_unit_title(
                    unit, self.app_context)),
            'display_lesson_title': (
                lambda unit, lesson: resources_display.display_lesson_title(
                    unit, lesson, self.app_context))})

        return template_environ.get_template(template_file)


class BaseHandler(CourseHandler):
    """Base handler."""

    def __init__(self, *args, **kwargs):
        super(BaseHandler, self).__init__(*args, **kwargs)
        self._old_locale = None

    def before_method(self, verb, path):
        """Modify global locale value for the duration of this handler."""
        self._old_locale = self.app_context.get_current_locale()
        new_locale = self.get_locale_for(self.request, self.app_context)
        self.app_context.set_current_locale(new_locale)

    def after_method(self, verb, path):
        """Restore original global locale value."""
        self.app_context.set_current_locale(self._old_locale)

    def personalize_page_and_get_user(self):
        """If the user exists, add personalized fields to the navbar."""
        user = self.get_user()
        PageInitializerService.get().initialize(self.template_value)

        if hasattr(self, 'app_context'):
            self.template_value['can_register'] = self.app_context.get_environ(
                )['reg_form']['can_register']

        if user:
            student = Student.get_enrolled_student_by_user(user)
            if student:
                student.update_last_seen_on()

            email = user.email()
            self.template_value['email_no_domain_name'] = (
                email[:email.find('@')] if '@' in email else email)
            self.template_value['email'] = email
            self.template_value['logoutUrl'] = (
                users.create_logout_url(self.request.uri))
            self.template_value['transient_student'] = False

            # configure page events
            self.template_value['record_tag_events'] = (
                CAN_PERSIST_TAG_EVENTS.value)
            self.template_value['record_page_events'] = (
                CAN_PERSIST_PAGE_EVENTS.value)
            self.template_value['record_events'] = (
                CAN_PERSIST_ACTIVITY_EVENTS.value)
            self.template_value['event_xsrf_token'] = (
                XsrfTokenManager.create_xsrf_token('event-post'))
        else:
            self.template_value['loginUrl'] = users.create_login_url(
                self.request.uri)
            self.template_value['transient_student'] = True
            return None

        return user

    def personalize_page_and_get_enrolled(
        self, supports_transient_student=False):
        """If the user is enrolled, add personalized fields to the navbar."""
        user = self.personalize_page_and_get_user()
        if user is None:
            student = TRANSIENT_STUDENT
        else:
            student = Student.get_enrolled_student_by_user(user)
            if not student:
                self.template_value['transient_student'] = True
                student = TRANSIENT_STUDENT

        if student.is_transient:
            if supports_transient_student and (
                    self.app_context.get_environ()['course']['browsable']):
                return TRANSIENT_STUDENT
            elif user is None:
                self.redirect(
                    users.create_login_url(self.request.uri), normalize=False
                )
                return None
            else:
                self.redirect('/preview')
                return None

        return student

    def assert_xsrf_token_or_fail(self, request, action):
        """Asserts the current request has proper XSRF token or fails."""
        token = request.get('xsrf_token')
        if not token or not XsrfTokenManager.is_xsrf_token_valid(token, action):
            self.error(403)
            return False
        return True

    @appengine_config.timeandlog('BaseHandler.render')
    def render(self, template_file, additional_dirs=None, save_location=True):
        """Renders a template."""
        prefs = models.StudentPreferencesDAO.load_or_default()

        courses.Course.set_current(self.get_course())
        models.MemcacheManager.begin_readonly()
        try:
            template = self.get_template(
                template_file, additional_dirs=additional_dirs, prefs=prefs)
            self.response.out.write(template.render(self.template_value))
        finally:
            models.MemcacheManager.end_readonly()
            courses.Course.clear_current()

        # If the page displayed successfully, save the location for registered
        # students so future visits to the course's base URL sends the student
        # to the most-recently-visited page.
        # TODO(psimakov): method called render() must not have mutations
        if save_location and self.request.method == 'GET':
            user = self.get_user()
            if user:
                student = models.Student.get_enrolled_student_by_user(user)
                if student:
                    prefs.last_location = self.request.path_qs
                    models.StudentPreferencesDAO.save(prefs)

    def get_redirect_location(self, student):
        if (not student.is_transient and
            (self.request.path == self.app_context.get_slug() or
             self.request.path == self.app_context.get_slug() + '/' or
             self.request.get('use_last_location'))):  # happens on '/' page
            prefs = models.StudentPreferencesDAO.load_or_default()
            # Belt-and-suspenders: prevent infinite self-redirects
            if (prefs and
                prefs.last_location and
                prefs.last_location != self.request.path_qs):
                return prefs.last_location
        return None


class BaseRESTHandler(CourseHandler, RESTHandlerMixin):
    """Base REST handler."""

    def __init__(self, *args, **kwargs):
        super(BaseRESTHandler, self).__init__(*args, **kwargs)

    def assert_xsrf_token_or_fail(self, token_dict, action, args_dict):
        """Asserts that current request has proper XSRF token or fails."""
        token = token_dict.get('xsrf_token')
        if not token or not XsrfTokenManager.is_xsrf_token_valid(token, action):
            transforms.send_json_response(
                self, 403,
                'Bad XSRF token. Please reload the page and try again',
                args_dict)
            return False
        return True

    def validation_error(self, message, key=None):
        """Deliver a validation message."""
        if key:
            transforms.send_json_response(
                self, 412, message, payload_dict={'key': key})
        else:
            transforms.send_json_response(self, 412, message)


class PreviewHandler(BaseHandler):
    """Handler for viewing course preview."""

    def get(self):
        """Handles GET requests."""
        user = self.personalize_page_and_get_user()
        if user is None:
            student = TRANSIENT_STUDENT
        else:
            student = Student.get_enrolled_student_by_user(user)
            if not student:
                student = TRANSIENT_STUDENT

        # If the course is browsable, or the student is logged in and
        # registered, redirect to the main course page.
        if ((student and not student.is_transient) or
            self.app_context.get_environ()['course']['browsable']):
            self.redirect('/course')
            return

        self.template_value['transient_student'] = True
        self.template_value['can_register'] = self.app_context.get_environ(
            )['reg_form']['can_register']
        self.template_value['navbar'] = {'course': True}
        self.template_value['units'] = self.get_units()
        self.template_value['show_registration_page'] = True

        course = self.app_context.get_environ()['course']
        self.template_value['video_exists'] = bool(
            'main_video' in course and
            'url' in course['main_video'] and
            course['main_video']['url'])
        self.template_value['image_exists'] = bool(
            'main_image' in course and
            'url' in course['main_image'] and
            course['main_image']['url'])

        if user:
            profile = StudentProfileDAO.get_profile_by_user_id(user.user_id())
            additional_registration_fields = self.app_context.get_environ(
                )['reg_form']['additional_registration_fields']
            if profile is not None and not additional_registration_fields:
                self.template_value['show_registration_page'] = False
                self.template_value['register_xsrf_token'] = (
                    XsrfTokenManager.create_xsrf_token('register-post'))
        self.render('preview.html')


class RegisterHandler(BaseHandler):
    """Handler for course registration."""

    # Hooks provide a mechanism to prevent students from registering.  Hooks
    # are called with the current application context and the user_id trying to
    # register.  If registration should be prevented, the hook should return
    # a list of alternate content to display on the registration page instead
    # of the normal registration form.
    PREVENT_REGISTRATION_HOOKS = []

    def get(self):
        """Handles GET request."""
        user = self.personalize_page_and_get_user()
        if not user:
            self.redirect(
                users.create_login_url(self.request.uri), normalize=False)
            return

        student = Student.get_enrolled_student_by_user(user)
        if student:
            self.redirect('/course')
            return

        can_register = self.app_context.get_environ(
            )['reg_form']['can_register']
        if not can_register:
            self.redirect('/course#registration_closed')
            return

        # pre-fill nick name from the profile if available
        self.template_value['current_name'] = ''
        profile = StudentProfileDAO.get_profile_by_user_id(user.user_id())
        if profile and profile.nick_name:
            self.template_value['current_name'] = profile.nick_name

        self.template_value['navbar'] = {}
        self.template_value['transient_student'] = True
        self.template_value['register_xsrf_token'] = (
            XsrfTokenManager.create_xsrf_token('register-post'))

        alternate_content = []
        for hook in self.PREVENT_REGISTRATION_HOOKS:
            alternate_content.extend(hook(self.app_context, user.user_id()))
        self.template_value['alternate_content'] = alternate_content

        self.render('register.html')

    def post(self):
        """Handles POST requests."""
        user = self.personalize_page_and_get_user()
        if not user:
            self.redirect(
                users.create_login_url(self.request.uri), normalize=False)
            return

        if not self.assert_xsrf_token_or_fail(self.request, 'register-post'):
            return

        can_register = self.app_context.get_environ(
            )['reg_form']['can_register']
        if not can_register:
            self.redirect('/course#registration_closed')
            return

        if 'name_from_profile' in self.request.POST.keys():
            profile = StudentProfileDAO.get_profile_by_user_id(user.user_id())
            name = profile.nick_name
        else:
            name = self.request.get('form01')

        Student.add_new_student_for_current_user(
            name, transforms.dumps(self.request.POST.items()), self,
            labels=self.request.get('labels'))

        # Render registration confirmation page
        self.redirect('/course#registration_confirmation')


class ForumHandler(BaseHandler):
    """Handler for forum page."""

    def get(self):
        """Handles GET requests."""
        student = self.personalize_page_and_get_enrolled(
            supports_transient_student=True)
        if not student:
            return

        self.template_value['navbar'] = {'forum': True}
        self.render('forum.html')


class StudentProfileHandler(BaseHandler):
    """Handles the click to 'Progress' link in the nav bar."""

    # A list of functions which will provide extra rows in the Student Progress
    # table. Each function will be passed the current handler, student,  and
    # course object and should return a pair of strings; the first being the
    # title of the data and the second the value to display.
    EXTRA_STUDENT_DATA_PROVIDERS = []

    # A list of callbacks which provides extra sections to the Student Progress
    # page. Each callback is passed the current handler, the app_context,
    # and the student and returns a jinja2.Markup or a safe_dom object.
    EXTRA_PROFILE_SECTION_PROVIDERS = []

    def get(self):
        """Handles GET requests."""
        student = self.personalize_page_and_get_enrolled()
        if not student:
            return

        track_labels = models.LabelDAO.get_all_of_type(
            models.LabelDTO.LABEL_TYPE_COURSE_TRACK)

        course = self.get_course()
        units = []
        for unit in course.get_units():
            # Don't show assessments that are part of units.
            if course.get_parent_unit(unit.unit_id):
                continue
            units.append({
                'unit_id': unit.unit_id,
                'title': unit.title,
                'labels': list(course.get_unit_track_labels(unit)),
                })

        name = student.name
        profile = student.profile
        if profile:
            name = profile.nick_name
        student_labels = student.get_labels_of_type(
            models.LabelDTO.LABEL_TYPE_COURSE_TRACK)
        self.template_value['navbar'] = {'progress': True}
        self.template_value['student'] = student
        self.template_value['student_name'] = name
        self.template_value['date_enrolled'] = student.enrolled_on.strftime(
            HUMAN_READABLE_DATE_FORMAT)
        self.template_value['score_list'] = course.get_all_scores(student)
        self.template_value['overall_score'] = course.get_overall_score(student)
        self.template_value['student_edit_xsrf_token'] = (
            XsrfTokenManager.create_xsrf_token('student-edit'))
        self.template_value['can_edit_name'] = (
            not models.CAN_SHARE_STUDENT_PROFILE.value)
        self.template_value['track_labels'] = track_labels
        self.template_value['student_labels'] = student_labels
        self.template_value['units'] = units
        self.template_value['track_env'] = transforms.dumps({
            'label_ids': [label.id for label in track_labels],
            'units': units
            })

        # Append any extra data which is provided by modules
        extra_student_data = []
        for data_provider in self.EXTRA_STUDENT_DATA_PROVIDERS:
            extra_student_data.append(data_provider(self, student, course))
        self.template_value['extra_student_data'] = extra_student_data

        profile_sections = []
        for profile_section_provider in self.EXTRA_PROFILE_SECTION_PROVIDERS:
            section = profile_section_provider(self, self.app_context, student)
            if section:
                profile_sections.append(section)
        self.template_value['profile_sections'] = profile_sections

        self.render('student_profile.html')


class StudentEditStudentHandler(BaseHandler):
    """Handles edits to student records by students."""

    def post(self):
        """Handles POST requests."""
        student = self.personalize_page_and_get_enrolled()
        if not student:
            return

        if not self.assert_xsrf_token_or_fail(self.request, 'student-edit'):
            return

        Student.rename_current(self.request.get('name'))

        self.redirect('/student/home')


class StudentSetTracksHandler(BaseHandler):
    """Handles submission of student tracks selections."""

    def post(self):
        student = self.personalize_page_and_get_enrolled()
        if not student:
            return
        if not self.assert_xsrf_token_or_fail(self.request, 'student-edit'):
            return

        all_track_label_ids = models.LabelDAO.get_set_of_ids_of_type(
            models.LabelDTO.LABEL_TYPE_COURSE_TRACK)
        new_track_label_ids = set(
            [int(label_id)
             for label_id in self.request.get_all('labels')
             if label_id and int(label_id) in all_track_label_ids])
        student_label_ids = set(
            [int(label_id)
             for label_id in common_utils.text_to_list(student.labels)
             if label_id])

        # Remove all existing track (and only track) labels from student,
        # then merge in selected set from form.
        student_label_ids = student_label_ids.difference(all_track_label_ids)
        student_label_ids = student_label_ids.union(new_track_label_ids)
        models.Student.set_labels_for_current(
            common_utils.list_to_text(list(student_label_ids)))

        self.redirect('/student/home')


class StudentUnenrollHandler(BaseHandler):
    """Handler for students to unenroll themselves."""

    # Each hook in this list is excuted on get().  Hooks are passed the
    # application context for the request.  They return a list of items which
    # are included in the unenroll_confirmation_check.html page within the
    # context of the form users submit to enroll.
    GET_HOOKS = []

    # Hooks in this list are executed on the POST of the form generated by
    # get().  It is expected that based on the values of additional fields
    # added to the form by GET_HOOKS, various extension modules will need to
    # hijack the flow of control.  For example, on unenroll, the data_deletion
    # module wants to put in an extra page to confirm removal of all user data
    # (if the relevant checkbox was checked).
    #
    # In order to support flow hijacking and continuation, the method
    # unenroll_post_continue() is provided.  When an item in POST_HOOKS
    # is called, it is provided with three items:
    # - The Student object corresponding to the logged-in user
    # - Some handler inheriting from BaseHandler (not necessarily
    #   a StudentUnenrollHandler instance - see below)
    # - A list of form parameters - a list of key, value 2-tuples.
    #   Hooks will probably want to instantiate a MultiDict instance to
    #   provide the familiar API for accessing parameters.  See
    #   unenroll_post_continue(), below, for an example of how.
    #
    # If a POST_HOOK entry decides it needs to hijack the flow of pages
    # presented to the user, on completion, that hook should finish by
    # calling back to unenroll_post_continue(), providing two values:
    # - A Handler object, usually the instance handling the final POST in the
    #   hijacking flow
    # - The same set of form parameters provided to the hook in the callback.
    #   (Hooks will probably want to JSON serialize the parameters value
    #   to simplify retention for use on flow completion)
    # Note: If the flow has controls that permit abandoning the unenroll,
    # it is _not_ necessary to call back to unenroll_post_continue(); just
    # redirect the user back to 'student/home'.
    #
    # See modules/data_removal.py for a worked example of how to handle this
    # flow control.
    #
    # Yes, this is overly complex and not very intuitive.  If/when we get to
    # a point where we have a use case for multi-page flows with sub-flows,
    # an appropriate framework should be applied here as well.
    #
    POST_HOOKS = collections.OrderedDict()
    _POST_HOOK_NAMES_PARAM = 'post_hook_names'

    def get(self):
        """Handles GET requests."""
        student = self.personalize_page_and_get_enrolled()
        if not student:
            return
        self.template_value['student'] = student
        self.template_value['navbar'] = {}
        self.template_value['student_unenroll_xsrf_token'] = (
            XsrfTokenManager.create_xsrf_token('student-unenroll'))
        hook_items = []
        for hook in self.GET_HOOKS:
            hook_items.extend(hook(self.app_context))
        self.template_value['hook_items'] = hook_items
        self.render('unenroll_confirmation_check.html')

    def post(self):
        """Handles POST requests."""
        # First time into the POST handling, add all the names of the hooks
        # we need to do.  As these are done, we will remove them one-by-one.

        # pylint: disable=abstract-class-instantiated
        parameters = multidict.MultiDict(self.request.params.items())
        for post_hook_name in self.POST_HOOKS.iterkeys():
            parameters.add(self._POST_HOOK_NAMES_PARAM, post_hook_name)
        self.unenroll_post_continue(self, parameters)

    @classmethod
    def unenroll_post_continue(cls, handler, parameters_list):
        student = handler.personalize_page_and_get_enrolled()
        if not student:
            return
        # pylint: disable=abstract-class-instantiated
        parameters = multidict.MultiDict(parameters_list)
        if not handler.assert_xsrf_token_or_fail(parameters,
                                                 'student-unenroll'):
            return

        # Before calling each hook, remove the name of the hook from the
        # parameters list, so that on callback by a hijacking flow, we
        # don't call that same hook again.
        hook_name = parameters.pop(cls._POST_HOOK_NAMES_PARAM, None)
        while hook_name:
            if cls.POST_HOOKS[hook_name](student, handler, parameters.items()):
                return
            hook_name = parameters.pop(cls._POST_HOOK_NAMES_PARAM, None)

        Student.set_enrollment_status_for_current(False)
        handler.template_value['navbar'] = {}
        handler.template_value['transient_student'] = True
        handler.render('unenroll_confirmation.html')


class StudentLocaleRESTHandler(BaseRESTHandler):
    """REST handler to manage student setting their preferred locale."""

    XSRF_TOKEN_NAME = 'locales'

    def post(self):
        request = transforms.loads(self.request.get('request'))
        if not self.assert_xsrf_token_or_fail(
                request, self.XSRF_TOKEN_NAME, {}):
            return

        selected = request['payload']['selected']
        if selected not in self.app_context.get_allowed_locales():
            transforms.send_json_response(self, 401, 'Bad locale')
            return

        prefs = models.StudentPreferencesDAO.load_or_default()
        if prefs:
            # Store locale in StudentPreferences for logged-in users
            prefs.locale = selected
            models.StudentPreferencesDAO.save(prefs)
        else:
            # Store locale in cookie for out-of-session users
            self.response.set_cookie(
                GUEST_LOCALE_COOKIE, selected,
                max_age=GUEST_LOCALE_COOKIE_MAX_AGE_SEC)

        transforms.send_json_response(self, 200, 'OK')
