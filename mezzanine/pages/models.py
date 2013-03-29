from django.core.urlresolvers import resolve, reverse
from django.db import models
from django.db.models import Q
from django.utils.translation import ugettext_lazy as _
from django.contrib.auth.models import Group, User
from django.core.mail import send_mail
from django.contrib.sites.models import Site
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.utils.timezone import now
from django.utils.formats import localize

from mezzanine.conf import settings
from mezzanine.core.models import Displayable, Orderable, RichText, \
    CONTENT_STATUS_DRAFT
from mezzanine.pages.fields import MenusField
from mezzanine.pages.managers import PageManager
from mezzanine.utils.urls import path_to_slug, slugify, admin_url


class BasePage(Orderable, Displayable):
    """
    Exists solely to store ``PageManager`` as the main manager.
    If it's defined on ``Page``, a concrete model, then each
    ``Page`` subclass loses the custom manager.
    """

    objects = PageManager()

    class Meta:
        abstract = True


class Page(BasePage):
    """
    A page in the page tree. This is the base class that custom content types
    need to subclass.
    """

    parent = models.ForeignKey("Page", blank=True, null=True,
        related_name="children")
    in_menus = MenusField(_("Show in menus"), blank=True, null=True)
    titles = models.CharField(editable=False, max_length=1000, null=True)
    content_model = models.CharField(editable=False, max_length=50, null=True)
    login_required = models.BooleanField(_("Login required"),
        help_text=_("If checked, only logged in users can view this page"))

    class Meta:
        verbose_name = _("Page")
        verbose_name_plural = _("Pages")
        ordering = ("titles",)
        order_with_respect_to = "parent"

    def __unicode__(self):
        return self.titles

    def get_absolute_url(self):
        """
        URL for a page - for ``Link`` page types, simply return its
        slug since these don't have an actual URL pattern. Also handle
        the special case of the homepage being a page object.
        """
        slug = self.slug
        if self.content_model == "link":
            # Ensure the URL is absolute.
            if not slug.lower().startswith("http"):
                slug = "/" + self.slug.lstrip("/")
            return slug
        if slug == "/":
            return reverse("home")
        else:
            return reverse("page", kwargs={"slug": slug})

    def save(self, *args, **kwargs):
        """
        Create the titles field using the titles up the parent chain
        and set the initial value for ordering.
        """
        if self.id is None:
            self.content_model = self._meta.object_name.lower()
        titles = [self.title]
        parent = self.parent
        while parent is not None:
            titles.insert(0, parent.title)
            parent = parent.parent
        self.titles = " / ".join(titles)
        super(Page, self).save(*args, **kwargs)

    def description_from_content(self):
        """
        Override ``Displayable.description_from_content`` to load the
        content type subclass for when ``save`` is called directly on a
        ``Page`` instance, so that all fields defined on the subclass
        are available for generating the description.
        """
        if self.__class__ == Page:
            content_model = self.get_content_model()
            if content_model:
                return content_model.description_from_content()
        return super(Page, self).description_from_content()

    def get_ascendants(self, for_user=None):
        """
        Returns the ascendants for the page. Ascendants are cached in
        the ``_ascendants`` attribute, which is populated when the page
        is loaded via ``Page.objects.with_ascendants_for_slug``.
        """
        if not self.parent_id:
            # No parents at all, bail out.
            return []
        if not hasattr(self, "_ascendants"):
            # _ascendants has not been either page.get_ascendants or
            # Page.objects.assigned by with_ascendants_for_slug, so
            # run it to see if we can retrieve all parents in a single
            # query, which will occur if the slugs for each of the pages
            # have not been customised.
            if self.slug:
                kwargs = {"for_user": for_user}
                pages = Page.objects.with_ascendants_for_slug(self.slug,
                                                              **kwargs)
                self._ascendants = pages[0]._ascendants
            else:
                self._ascendants = []
        if not self._ascendants:
            # Page has a parent but with_ascendants_for_slug failed to
            # find them due to custom slugs, so retrieve the parents
            # recursively.
            child = self
            while child.parent_id is not None:
                self._ascendants.append(child.parent)
                child = child.parent
        return self._ascendants

    @classmethod
    def get_content_models(cls):
        """
        Return all Page subclasses.
        """
        is_content_model = lambda m: m is not Page and issubclass(m, Page)
        return filter(is_content_model, models.get_models())

    def get_content_model(self):
        """
        Provies a generic method of retrieving the instance of the custom
        content type's model for this page.
        """
        return getattr(self, self.content_model, None)

    def get_slug(self):
        """
        Recursively build the slug from the chain of parents.
        """
        slug = slugify(self.title)
        if self.parent is not None:
            return "%s/%s" % (self.parent.slug, slug)
        return slug

    def set_slug(self, new_slug):
        """
        Changes this page's slug, and all other pages whose slugs
        start with this page's slug.
        """
        for page in Page.objects.filter(slug__startswith=self.slug):
            if not page.overridden():
                page.slug = new_slug + page.slug[len(self.slug):]
                page.save()
        self.slug = new_slug

    def set_parent(self, new_parent):
        """
        Change the parent of this page, changing this page's slug to match
        the new parent if necessary.
        """
        self_slug = self.slug
        old_parent_slug = self.parent.slug if self.parent else ""
        new_parent_slug = new_parent.slug if new_parent else ""

        # Make sure setting the new parent won't cause a cycle.
        parent = new_parent
        while parent is not None:
            if parent.pk == self.pk:
                raise AttributeError("You can't set a page or its child as"
                                     " a parent.")
            parent = parent.parent

        self.parent = new_parent
        self.save()

        if self_slug:
            if not old_parent_slug:
                self.set_slug("/".join((new_parent_slug, self.slug)))
            elif self.slug.startswith(old_parent_slug):
                new_slug = self.slug.replace(old_parent_slug,
                                             new_parent_slug, 1)
                self.set_slug(new_slug.strip("/"))

    def overridden(self):
        """
        Returns ``True`` if the page's slug has an explicitly defined
        urlpattern and is therefore considered to be overridden.
        """
        from mezzanine.pages.views import page
        page_url = reverse("page", kwargs={"slug": self.slug})
        resolved_view = resolve(page_url)[0]
        return resolved_view != page

    def can_add(self, request):
        """
        Dynamic ``add`` permission for content types to override.
        """
        return self.slug != "/"

    def can_change(self, request):
        """
        Dynamic ``change`` permission for content types to override.
        """
        return True

    def can_delete(self, request):
        """
        Dynamic ``delete`` permission for content types to override.
        """
        return True

    def set_helpers(self, context):
        """
        Called from the ``page_menu`` template tag and assigns a
        handful of properties based on the current page, that are used
        within the various types of menus.
        """
        current_page = context["_current_page"]
        current_page_id = getattr(current_page, "id", None)
        current_parent_id = getattr(current_page, "parent_id", None)
        # Am I a child of the current page?
        self.is_current_child = self.parent_id == current_page_id
        self.is_child = self.is_current_child  # Backward compatibility
        # Is my parent the same as the current page's?
        self.is_current_sibling = self.parent_id == current_parent_id
        # Am I the current page?
        try:
            request = context["request"]
        except KeyError:
            # No request context, most likely when tests are run.
            self.is_current = False
        else:
            self.is_current = self.slug == path_to_slug(request.path_info)

        # Is the current page me or any page up the parent chain?
        def is_c_or_a(page_id):
            parent_id = context["_parent_page_ids"].get(page_id)
            return self.id == page_id or (parent_id and is_c_or_a(parent_id))
        self.is_current_or_ascendant = lambda: bool(is_c_or_a(current_page_id))
        # Am I a primary page?
        self.is_primary = self.parent_id is None
        # What's an ID I can use in HTML?
        self.html_id = self.slug.replace("/", "-")
        # Default branch level - gets assigned in the page_menu tag.
        self.branch_level = 0

    def in_menu_template(self, template_name):
        if self.in_menus is not None:
            for i, l, t in settings.PAGE_MENU_TEMPLATES:
                if not unicode(i) in self.in_menus and t == template_name:
                    return False
        return True


class Moderated(models.Model):

    PENDING = 1
    APPROVED = 2
    REJECTED = 3

    APPROVAL_STATES = (
        (PENDING, _("Pending")),
        (APPROVED, _("Approved")),
        (REJECTED, _("Rejected")),
    )

    approval = models.IntegerField(choices=APPROVAL_STATES, null=True,
        editable=False)
    owning_group = models.ForeignKey(Group, verbose_name=_("Owning group"),
        limit_choices_to=~(Q(name="Author") | Q(name="Publisher")),
        blank=True, null=True)

    class Meta:
        abstract = True

    def can_add(self, request):
        return request.user.groups.filter(name="Author").exists()

    @property
    def admin_url(self):
        return admin_url(self.__class__, "change", self.pk)

    def send_pending_email(self):
        _type = self._meta.verbose_name.lower()
        subject = "Draft %s needs moderation" % _type

        site = Site.objects.get_current()
        body = "http://%s%s" % (site.domain, self.admin_url)

        publishers = []
        q = Q(groups__name="Publisher")
        if self.owning_group:
            publishers = User.objects.filter(groups=self.owning_group)\
                .filter(q)
        publishers = publishers or User.objects.filter(q)
        to = publishers.values_list("email", flat=True).distinct()

        _from = getattr(settings, "DEFAULT_FROM_EMAIL", None)

        send_mail(subject, body, _from, to, fail_silently=not(settings.DEBUG))

        return publishers

    @property
    def current_status(self):
        if self.status == CONTENT_STATUS_DRAFT:
            if self.approval == Moderated.PENDING:
                return ("pending", "Pending approval")
            elif self.approval == Moderated.REJECTED:
                return ("rejected", "Rejected")
            return ("draft", "Draft")
        pdate = localize(self.publish_date)
        if self.publish_date < now():
            return ("published", "Approved, published on %s" % pdate)
        return ("approved", "Approved, to be published on %s" % pdate)


class ModeratedPage(Page, Moderated):

    class Meta:
        verbose_name = _("Moderated page")
        verbose_name_plural = _("Moderated pages")


class RichTextPage(Page, RichText):
    """
    Implements the default type of page with a single Rich Text
    content field.
    """

    class Meta:
        verbose_name = _("Rich text page")
        verbose_name_plural = _("Rich text pages")


class Link(Page):
    """
    A general content type for creating external links in the page
    menu.
    """

    class Meta:
        verbose_name = _("Link")
        verbose_name_plural = _("Links")


class Workflow(models.Model):
    # User who made the change
    change_user = models.ForeignKey(User, related_name="changes_made")
    # Users on whose task list this item should be shown
    task_users = models.ManyToManyField(User, related_name="tasks_for",
        blank=True, null=True)
    status = models.CharField(max_length=255)

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey('content_type', 'object_id')
