import calendar
import datetime
import hashlib
import logging
import os

from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.conf import settings
from django.core.files import File

from instances.models import InstanceMixin, InstanceManager
import speeches
from speeches.utils import AudioHelper

from djqmethod import Manager, querymethod
from popit.models import Person

logger = logging.getLogger(__name__)

class cache(object):
    '''Computes attribute value and caches it in the instance.
    Python Cookbook (Denis Otkidach) http://stackoverflow.com/users/168352/denis-otkidach
    This decorator allows you to create a property which can be computed once and
    accessed many times. Sort of like memoization.

    '''
    def __init__(self, method, name=None):
        # record the unbound-method and the name
        self.method = method
        self.name = name or method.__name__
        self.__doc__ = method.__doc__
    def __get__(self, inst, cls):
        # self: <__main__.cache object at 0xb781340c>
        # inst: <__main__.Foo object at 0xb781348c>
        # cls: <class '__main__.Foo'>
        if inst is None:
            # instance attribute accessed on class, return self
            # You get here if you write `Foo.bar`
            return self
        # compute, cache and return the instance's attribute value
        result = self.method(inst)
        # setattr redefines the instance's attribute so this doesn't get called again
        setattr(inst, self.name, result)
        return result

class AuditedModel(models.Model):
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        now = timezone.now()
        if not self.id:
            self.created = now
        self.modified = now
        super(AuditedModel, self).save(*args, **kwargs)

# Speaker - someone who gave a speech
class Speaker(InstanceMixin, AuditedModel):
    person = models.ForeignKey(Person, blank=True, null=True, on_delete=models.PROTECT)
    name = models.TextField(db_index=True)

    class Meta:
        ordering = ('name',)

    def __unicode__(self):
        if self.name:
            return self.name
        return "[no name]"

    def colour(self):
        return hashlib.sha1('%s' % (self.person_id or self.id)).hexdigest()[:6]

    @models.permalink
    def get_absolute_url(self):
        return ( 'speaker-view', (), { 'pk': self.id } )

    @models.permalink
    def get_edit_url(self):
        return ( 'speaker-edit', (), { 'pk': self.id } )

    # http://stackoverflow.com/a/5772272/669631
    def save(self, *args, **kwargs):
        if self.person:
            conflicting_instance = Speaker.objects.filter(instance=self.instance, person=self.person)
            if self.id:
                conflicting_instance = conflicting_instance.exclude(pk=self.id)
            if conflicting_instance.exists():
                raise Exception('Speaker with this instance and popit person already exists.')
        super(Speaker, self).save(*args, **kwargs)


class Tag(InstanceMixin, AuditedModel):
    name = models.CharField(unique=True, max_length=100)

    def __unicode__(self):
        return self.name


# Speech manager
class SpeechManager(InstanceManager, Manager):

    def create_from_recording(self, recording, instance):
        """Create one or more speeches from a recording. If there's no audio"""
        created_speeches = []

        # Split the recording's audio files
        audio_helper = AudioHelper()
        audio_files = audio_helper.split_recording(recording)

        # Create a speech for each one of the audio files
        sorted_timestamps = recording.timestamps.order_by("timestamp")
        for index, audio_file in enumerate(audio_files):
            speaker = None
            start_date = None
            start_time = None
            end_date = None
            end_time = None

            # Get the related timestamp, if any.
            timestamp = None
            if sorted_timestamps and len(sorted_timestamps) > 0:
                # We assume that the files are returned in order of timestamp
                timestamp = sorted_timestamps[index]
                speaker = timestamp.speaker
                start_date = timestamp.timestamp.date()
                start_time = timestamp.timestamp.time()
                # If there's another one we can work out the end too
                if index < len(sorted_timestamps) - 1:
                    next_timestamp = sorted_timestamps[index + 1]
                    end_date = next_timestamp.timestamp.date()
                    end_time = next_timestamp.timestamp.time()

            new_speech = self.create(
                instance = instance,
                public = False,
                audio=File(open(audio_file)),
                speaker=speaker,
                start_date=start_date,
                start_time=start_time,
                end_date=end_date,
                end_time=end_time
            )
            created_speeches.append( new_speech )
            if timestamp:
                timestamp.speech = new_speech
                timestamp.save()

        return created_speeches


class Section(AuditedModel, InstanceMixin):
    title = models.CharField(max_length=255, blank=False, null=False)
    parent = models.ForeignKey('self', null=True, blank=True, related_name='children')

    class Meta:
        ordering = ('id',)

    def __unicode__(self):
        return self.title

    def speech_datetimes(self):
        return (datetime.datetime.combine(s.start_date, s.start_time or datetime.time(0,0) )
                for s in self.speech_set.all())

    def is_leaf_node(self):
        return not self.children.exists()

    @cache
    def get_children(self):
        tree = self.get_descendants
        try:
            lvl = tree[0].level
            return [ s for s in tree if s.level == lvl ]
        except:
            return []

    @cache
    def get_ancestors(self):
        # Cached for now, so these aren't supplied as arguments
        ascending = False
        include_self = True
        """Return the ancestors of the current Section, in either direction,
        optionally including itself."""
        dir = ascending and 'ASC' or 'DESC'
        s = Section.objects.raw(
            """WITH RECURSIVE cte AS (
                SELECT speeches_section.*, 1 AS l FROM speeches_section WHERE id=%s
                UNION ALL
                SELECT s.*, l+1 FROM cte JOIN speeches_section s ON cte.parent_id = s.id
            )
            SELECT * FROM cte ORDER BY l """ + dir,
            [ self.id ]
        )
        if not include_self:
            s = ascending and s[1:] or s[:-1]
        return list(s) # So it's evaluated and will be cached

    def _get_descendants(self, include_self=False, include_count='', include_min='', max_depth=''):
        """Return the descendants of the current Section, in depth-first order.
        Optionally, include speech counts, minimum speech times, and only
        descend a certain depth."""
        select = [ '*' ]
        if include_count:
            select.append("(SELECT COUNT(*) FROM speeches_speech WHERE section_id = cte.id) AS speech_count")
        if include_min:
            select.append("(SELECT MIN( start_date + COALESCE(start_time, time '00:00') ) FROM speeches_speech WHERE section_id = cte.id) AS speech_min")
        if max_depth:
            max_depth = "WHERE array_upper(path, 1) < %d" % (max_depth+1)
        s = Section.objects.raw(
            """WITH RECURSIVE cte AS (
                SELECT speeches_section.*, 0 AS level, ARRAY[id] AS path FROM speeches_section WHERE id=%s
                UNION ALL
                SELECT s.*, level+1, cte.path||s.id FROM cte JOIN speeches_section s ON cte.id = s.parent_id
            """ + max_depth + """
            )
            SELECT """ + ','.join(select) + """ FROM cte ORDER BY path""",
            [ self.id ]
        )
        if not include_self:
            s = s[1:]
        return s

    @cache
    def get_descendants_tree(self):
        d = self._get_descendants_by_speech(include_count=True)
        prev_level = 0
        prev_path = None
        out = []
        for node in d:
            s = {}
            if node.level > prev_level:
                s['new_level'] = True
            elif node.level < prev_level:
                out[-1][1]['closed_levels'] = range(prev_level, node.level, -1)
            elif node.path[:-1] != prev_path[:-1]: # Swapping parentage in some way
                sw = ( i for i in xrange(len(node.path)) if node.path[i] != prev_path[i] ).next()
                out[-1][1]['closed_levels'] = range(node.level, node.level-sw, -1)
                s = {}
                for i in range(node.level-sw, node.level):
                    out.append( (Section.objects.get(id=node.path[i]), s) )
                    s = { 'new_level': True }
            prev_level, prev_path = node.level, node.path
            out.append( (node, s) )
        if out:
            out[-1][1]['closed_levels'] = range(prev_level, 0, -1)
        return out

    @cache
    def get_descendants(self):
        return self._get_descendants_by_speech()

    def _get_descendants_by_speech(self, **kwargs):
        dqs = self._get_descendants(include_min=True, **kwargs)

        earliest = {}
        for d in dqs:
            if not d.speech_min:
                continue
            for parent in reversed(d.path):
                if parent not in earliest or d.speech_min < earliest[parent]:
                    earliest[parent] = d.speech_min

        return sorted( dqs, key=lambda s: earliest.get(s.id, datetime.datetime(datetime.MAXYEAR, 12, 31)) )

    @models.permalink
    def get_absolute_url(self):
        return ( 'section-view', (), { 'pk': self.id } )

    @models.permalink
    def get_edit_url(self):
        return ( 'section-edit', (), { 'pk': self.id } )

    def _get_next_previous_node(self, direction):
        if not self.parent:
            return None
        root = self.get_ancestors[0]
        tree = root.get_descendants
        idx = tree.index(self)
        lvl = tree[idx].level
        same_level = [ s for s in tree if s.level == lvl ]
        idx = same_level.index(self)
        if direction == -1 and idx == 0: return None
        try:
            return same_level[idx+direction]
        except:
            return None

    def get_next_node(self):
        """Fetch the next node in the tree, at the same level as this one."""
        return self._get_next_previous_node(1)

    def get_previous_node(self):
        """Fetch the previous node in the tree, at the same level as this one."""
        return self._get_next_previous_node(-1)

# Speech that a speaker gave
class Speech(InstanceMixin, AuditedModel):
    # Custom manager
    objects = SpeechManager()

    # The speech. Need to check have at least one of the following two (preferably both).
    audio = models.FileField(upload_to='speeches/%Y-%m-%d/', max_length=255, blank=True)
    # TODO - we will want to do full text search at some point, so we need an index on
    # this field in some way, but The Right Thing looks complicated, and the current method breaks
    # on really big text.  Since we don't have search at all at the moment, I've removed
    # the basic index completely for now.
    text = models.TextField(blank=True, db_index=False, help_text='The text of the speech')

    # The section that this speech is part of
    section = models.ForeignKey(Section, blank=True, null=True, help_text='The section that this speech is contained in')
    title = models.TextField(blank=True, help_text='The title of the speech, if relevant')
    # The below two fields could be on the section if we made it a required field of a speech
    event = models.TextField(db_index=True, blank=True, help_text='Was the speech at a particular event?')
    location = models.TextField(db_index=True, blank=True, help_text='Where the speech took place')

    # Metadata on the speech
    # type = models.ChoiceField() # speech, scene, narrative, summary, etc.
    speaker = models.ForeignKey(Speaker, blank=True, null=True, help_text='Who gave this speech?', on_delete=models.SET_NULL)
    start_date = models.DateField(blank=True, null=True, help_text='What date did the speech start?')
    start_time = models.TimeField(blank=True, null=True, help_text='What time did the speech start?')
    end_date = models.DateField(blank=True, null=True, help_text='What date did the speech end?')
    end_time = models.TimeField(blank=True, null=True, help_text='What time did the speech end?')
    tags = models.ManyToManyField(Tag, blank=True, null=True)

    public = models.BooleanField(default=True, help_text='Is this speech public?')

    # What if source material has multiple speeches, same timestamp - need a way of ordering them?
    # pos = models.IntegerField()

    source_url = models.TextField(blank=True)
    # source_column? Any other source based things?

    # Task id for celery transcription tasks
    celery_task_id = models.CharField(max_length=256, null=True, blank=True)

    class Meta:
        verbose_name_plural = 'speeches'
        ordering = ( 'start_date', 'start_time', 'id' )

    def __unicode__(self):
        out = 'Speech'
        if self.title: out += ', %s,' % self.title
        if self.speaker: out += ' by %s' % self.speaker
        if self.start_date: out += ' at %s' % self.start_date
        if self.text: out += ' (with text)'
        if self.audio: out += ' (with audio)'
        return out

    @querymethod
    def visible(query, request=None):
        if not request.is_user_instance:
            query = query.filter(public=True)
        return query

    @property
    def is_public(self):
        return self.public

    @property
    def summary(self):
        summary_length = settings.SPEECH_SUMMARY_LENGTH
        default_transcription = settings.DEFAULT_TRANSCRIPTION
        if self.audio and (not self.text or self.text == default_transcription):
            return "[ recorded audio ]"
        else:
            return self.text[:summary_length] + '...' if len(self.text) > summary_length else self.text

    @models.permalink
    def get_absolute_url(self):
        return ( 'speech-view', (), { 'pk': self.id } )

    @models.permalink
    def get_edit_url(self):
        return ( 'speech-edit', (), { 'pk': self.id } )

    def save(self, *args, **kwargs):
        """Overriden save method to automatically convert the audio to an mp3"""

        # If we have an audio file and it's not an mp3, make it one
        if self.audio and not self.audio.name.lower().endswith('.mp3'):
            if not os.path.exists(self.audio.path):
                # If it doesn't already exist, save the old audio first so that we can re-encode it
                # This is needed if it's newly uploaded
                self.audio.save(self.audio.name, File(self.audio), False)
            # Transcode the audio into mp3
            audio_helper = speeches.utils.AudioHelper()
            mp3_filename = audio_helper.make_mp3(self.audio.path)
            mp3_file = open(mp3_filename, 'rb')
            # Delete the old file
            self.audio.delete(False)
            # Save the mp3 as the new file
            self.audio.save(mp3_file.name, File(mp3_file), False)

        # Call the original model save to do everything
        super(Speech, self).save(*args, **kwargs)

    def get_next_speech(self):
        """Return the next speech to this one in the same section, in a start
        date/time/ID ordering."""
        if self.start_date:
            # A later date, or no date present
            q1 = Q(start_date__gt=self.start_date) | Q(start_date__isnull=True)
            # Or, if on the same date...
            q2 = Q(start_date=self.start_date)
        else:
            # No dates later than our unknown
            q1 = Q()
            # But in that case...
            q2 = Q(start_date__isnull=True)
        if self.start_time:
            # If the time is later, or not present, or the same with a greater ID
            q3 = Q(start_time__gt=self.start_time) | Q(start_time__isnull=True) | Q(start_time=self.start_time, id__gt=self.id)
        else:
            # If the time is not present and the ID is greater
            q3 = Q(start_time__isnull=True, id__gt=self.id)
        q = q1 | ( q2 & q3 )

        if self.section:
            s = list(Speech.objects.filter(q, section=self.section)[:1])
        else:
            s = list(Speech.objects.filter(q, instance=self.instance, section__isnull=True)[:1])
        if s: return s[0]
        if self.section:
            next_section = self.section.get_next_node()
            if next_section:
                s = next_section.speech_set.all()[:1]
        return s and s[0] or None

    def get_previous_speech(self):
        """Return the previous speech to this one in the same section,
        in a start date/time/ID ordering."""
        if self.start_date:
            # A date less than ours
            q1 = Q(start_date__lt=self.start_date)
            # Or if the same date...
            q2 = Q(start_date=self.start_date)
        else:
            # Any speech with a date is earlier
            q1 = Q(start_date__isnull=False)
            # And for those other speeches with no date...
            q2 = Q(start_date__isnull=True)
        if self.start_time:
            # If the time is earlier, or the same and the ID is smaller
            q3 = Q(start_time__lt=self.start_time) | Q(start_time=self.start_time, id__lt=self.id)
        else:
            # If there is a time, or there isn't but the ID is smaller
            q3 = Q(start_time__isnull=False) | Q(start_time__isnull=True, id__lt=self.id)
        q = q1 | ( q2 & q3 )

        if self.section:
            s = list(Speech.objects.filter(q, section=self.section).reverse()[:1])
        else:
            s = list(Speech.objects.filter(q, instance=self.instance, section__isnull=True).reverse()[:1])
        if s: return s[0]
        if self.section:
            prev_section = self.section.get_previous_node()
            if prev_section:
                s = prev_section.speech_set.all().reverse()[:1]
        return s and s[0] or None

    def start_transcribing(self):
        """Kick off a celery task to transcribe this speech"""
        # We only do anything if there's no text already
        if not self.text:
            # If someone is adding a new audio file and there's already a task
            # We need to clear it
            if self.celery_task_id:
                celery.task.control.revoke(self.celery_task_id)
            # Now we can start a new one
            result = speeches.tasks.transcribe_speech.delay(self.id)
            # Finally, we can remember the new task in the model
            self.celery_task_id = result.task_id
            self.save()

# A timestamp of a particular speaker at a particular time.
# Used to record events like "This speaker started speaking at 00:33"
# in a specific recording, before it's chopped up into a speech
class RecordingTimestamp(InstanceMixin, AuditedModel):
    speaker = models.ForeignKey(Speaker, blank=True, null=True, on_delete=models.SET_NULL)
    timestamp = models.DateTimeField(db_index=True, blank=False)
    speech = models.ForeignKey(Speech, blank=True, null=True, on_delete=models.SET_NULL)
    recording = models.ForeignKey('Recording',blank=False, null=False, related_name='timestamps', default=0) # kludge default 0, should not be used

    class Meta:
        ordering = ('timestamp',)

    @property
    def utc(self):
        """Return our timestamp as a UTC long"""
        return calendar.timegm(self.timestamp.timetuple())


# A raw recording, might be divided up into multiple speeches
class Recording(InstanceMixin, AuditedModel):
    audio = models.FileField(upload_to='recordings/%Y-%m-%d/', max_length=255, blank=False)
    start_datetime = models.DateTimeField(blank=True, null=True, help_text='Datetime of first timestamp associated with recording')
    audio_duration = models.IntegerField(blank=True, null=False, default=0, help_text='Duration of recording, in seconds')

    def __unicode__(self):
        return u'Recording made on {date:%d %B %Y} at {date:%H:%M}'.format(date=self.created)

    @models.permalink
    def get_absolute_url(self):
        return ( 'recording-view', (), { 'pk': self.id } )

    def add_speeches_to_section(self, section):
        return Speech.objects.filter(recordingtimestamp__recording=self).update(section=section)
