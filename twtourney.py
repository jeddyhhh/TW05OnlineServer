"""The Online Tournaments wire format -- `OnlineTourn.c`.

    python twtourney.py            # self-test

Tournaments are the one part of the online menu that is NOT a TagField.  The
replies to `mg5ri` and `qdb@w` are plain ASCII hex, read by `0x002DDBD0`:

    0x002DDBF8  if src[0] < '0': stop
    0x002DDC08  if src[1] < '0': stop
    0x002DDC54  byte = high[src[0]] | low[src[1]]
    0x002DDC58  src += 2

`high` is the table at `0x00304480` and `low` the one at `0x00304580`.  Both
accept `0`-`9`, `A`-`F` and `a`-`f` and hold zero everywhere else, so the
terminator is any character below `'0'` -- a newline, a NUL, or the end of the
body.  Anything else that is not a hex digit decodes as a zero nibble rather
than ending the list, which is worth remembering: a stray letter corrupts the
rest of the stream instead of stopping it.

THE LIST (`0x002DDCC0`)

    XX                 entry count, one byte
      XX               name length, one byte
      <name>           that many LITERAL characters -- NOT hex
      32 hex chars     DATA_BYTES bytes of tournament data

The name really is copied straight out of the body: `0x002DDDF4` is a memcpy of
`namelen` bytes, and only then does the cursor skip `namelen` -- not `namelen *
2`.  Everything else in the list is hex.

An entry is 48 bytes in memory: the name at `+0x00`, memset to zero and then
filled to at most 32, and the 16 data bytes at `+0x20`.  `0x002DECE0` allocates
`0x13B0` bytes for the info list, so 105 entries is the ceiling, and the parser
refuses a count that would overrun it (`0x002DDD70`).

Of the 16 data bytes, one field is known: the **day, a 16-bit number at offset
12**, which is what the calendar looks a cell up by.  See DAY_OFFSET.  The other
fourteen are still unnamed, and `probe_days` is how to name them -- it puts a
real day where the client expects it, so a cell can actually be opened, and
fills everything else with its own offset.
"""
import datetime
import random
import struct
import sys

# `START` is a DATE, not a row.  The calendar asked for START=46266 NUM=30 while
# showing September 2026, and 46266 days after 1899-12-30 is 2026-09-01 -- the
# OLE/Excel serial day, day 0 = 1899-12-30.  No other epoch lands on the 1st,
# and NUM=30 is exactly that month's length.
EPOCH = datetime.date(1899, 12, 30)

NAME_MAX = 32                    # memset 0x20 at entry+0x00, in both parsers

# THE TWO LISTS ARE NOT THE SAME WIDTH.  `0x002DDCC0` (info) asks the reader for
# 0x10 bytes and strides 0x30; `0x002DDEA0` (results) asks for 0x0C and strides
# 0x2C.  Sending 16-byte entries to the results list desynchronises the stream
# -- the four extra bytes are read as the next entry's name length and name --
# and the parser gives up.  Measured on the live client: 30 entries accepted on
# the info list, 0 on the results list, from one and the same body.
# TW05: the calendar entry grew.  0x001D4040 (the `tinfo` parser) reads a
# 2-digit count, then per entry a 2-digit name length, the name raw, and
# 0x3C = 60 data bytes, striding 0x5C in memory (name 0x20 + data 0x3C); the
# array is 0x2560 bytes, so 104 entries.  The results list (`tdwin`,
# 0x001D41C0) is unchanged: 12 data bytes, stride 0x2C, 0x11E0 bytes.
DATA_BYTES = 60                  # TW05 0x001D4160 (TW04: 16)
ENTRY_SIZE = 92                  # TW05 0x001D4198 (TW04: 48)
MAX_ENTRIES = 0x2560 // ENTRY_SIZE      # 104 (TW04: 105)

RESULT_BYTES = 12                # 0x002DE000
RESULT_ENTRY_SIZE = 44           # 0x002DE054
MAX_RESULTS = 0x120C // RESULT_ENTRY_SIZE       # 105, allocated at 0x002DED34

HEX = '0123456789ABCDEF'


def encode_bytes(data):
    """Bytes as the hex the reader expects.  Upper case; either case decodes."""
    return ''.join('%02X' % b for b in bytearray(data))


def decode_bytes(text, count):
    """`count` bytes out of `text`, stopping exactly where 0x002DDBD0 stops.

    Returns (bytes, characters_consumed).  A short read is not an error here --
    the caller checks, the same way the parser does.
    """
    out = bytearray()
    i = 0
    while len(out) < count:
        if i + 1 >= len(text) or text[i] < '0' or text[i + 1] < '0':
            break
        out.append((_nibble(text[i]) << 4) | _nibble(text[i + 1]))
        i += 2
    return bytes(out), i


def _nibble(c):
    """One hex digit, or zero -- the tables hold zero for everything else."""
    i = HEX.find(c.upper())
    return i if i >= 0 else 0


def encode_list(entries, width=DATA_BYTES):
    """[(name, data)] -> the body of an `mg5ri` (width 16) or `qdb@w` (12) reply.

    `data` is padded or truncated to `width` and the name to NAME_MAX, so a
    caller cannot accidentally desynchronise the stream.
    """
    if len(entries) > MAX_ENTRIES:
        raise ValueError('at most %d entries fit in the client array'
                         % MAX_ENTRIES)
    out = [encode_bytes([len(entries)])]
    for name, data in entries:
        name = _ascii(name)[:NAME_MAX]
        data = bytes(bytearray(data)[:width]).ljust(width, b'\0')
        out.append(encode_bytes([len(name)]))
        out.append(name)
        out.append(encode_bytes(data))
    return ''.join(out)


def decode_list(text, width=DATA_BYTES):
    """The inverse, for the self-test -- the client has no encoder to compare to."""
    count, used = decode_bytes(text, 1)
    if not count:
        return []
    text = text[used:]
    entries = []
    for _ in range(count[0]):
        n, used = decode_bytes(text, 1)
        if not n:
            raise ValueError('truncated: no name length')
        text = text[used:]
        length = n[0]
        name, text = text[:length], text[length:]
        data, used = decode_bytes(text, width)
        if len(data) != width:
            raise ValueError('truncated: %d of %d data bytes' % (len(data),
                                                                 width))
        text = text[used:]
        entries.append((name, data))
    return entries


def _ascii(name):
    """The PS2 font has nothing else, and a non-ASCII byte would shift the
    stream by a byte the parser did not count."""
    return ''.join(c if 32 <= ord(c) < 127 else '?' for c in name)


def to_day(when):
    """A date as the client counts them."""
    return (when - EPOCH).days


def from_day(n):
    return EPOCH + datetime.timedelta(days=n)


def today():
    return to_day(datetime.date.today())


# THE DAY IS A 16-BIT NUMBER AT DATA OFFSET 12 -- entry+0x2C.  Found by trapping
# a read of an entry's data bytes and landing in `0x002DEFF8`, which is "give me
# the tournament on day N":
#
#     0x002DF038  v1 = INFO_ARRAY
#     0x002DF03C  if day < *(u16 *)(v1 + 0x2C):            return 0   ; first
#     0x002DF060  if *(u16 *)(v1 + count*0x30 - 4) < day:  return 0   ; last
#     0x002DF088  for each entry: if *(u16 *)(e + 0x2C) == day: return e
#
# It range-checks the first and last entry before scanning, so **the list has to
# be sorted ascending by day** or everything outside that span is unreachable.
#
# TW05: the lookup is 0x001D56D0, and it reads `lhu 0x30(entry)` -- data
# offset 0x30 - 0x20 = 16.  The first/last range check is there too
# (0x001D56E8, 0x001D5708), so the list must still be sorted by day.
DAY_OFFSET = 16                  # TW04: 12
DAY_MAX = 0xFFFF                 # lhu, so the calendar runs out in 2079

# The rest of the entry, read off TODAY'S EVENT with every byte holding its own
# offset.  The screen drew:
#
#     PURSE:  $50,462,976     = 0x03020100, bytes 0..3 -> u32 at 0
#     DATE:   9/19/2026       = the day at 12, as above
#     COURSE: TORREY PINES    = index 14 in the course table, from the byte at 14
#
# so those three are certain.  HOLES, TEES, ROUGH, FAIRWAYS and GREENS also drew
# values, but every probe byte was non-zero and several of those enums are small
# enough to clamp, so which byte feeds which line is not yet pinned down.
#
# TW05: NOT YET MAPPED.  These are TW04's offsets, kept only so the code runs;
# the 60-byte TW05 entry is being mapped with --probe-layout.
PURSE_OFFSET = 0                 # u32
COURSE_OFFSET = 14               # u8, indexes twstats.COURSES

# Byte 15 looks like the CALENDAR ICON.  Under the one-byte-at-a-time probe the
# only day drawn with something other than a heart was the BYTE15=1 day, and
# every other day -- all carrying 0 there -- had the heart.  Which icon each
# number is has not been enumerated; `probe_icons` is how to do that.
ICON_OFFSET = 15
# TW05: DATA BYTE 24, a signed byte.  The calendar's per-day callback
# (0x0020AC20, reached through a table, never called directly) looks the day
# up, writes its status line, and returns `lb 0x38(entry)` through the
# accessor 0x001D6958 -- entry + 0x20 + 24.  A day with no event returns -1:
# no icon.  Every event sent 0 here until 2026-09-30, and 0 is the heart, so
# the whole calendar was hearts.
ICON_OFFSET_TW05 = 24

# TW05's calendar icons, read off the calendar with --probe-icons on
# 2026-09-30 (September: icon = date - 1; October: icon = date + 30).  There
# are 56: 0..29 are TW04's set, 31..55 are new in TW05, and 56 on draw
# nothing.  30 fell on 31 September, which does not exist, so it is unseen.
# September had already been played through, so 0..29 were drawn dimmed;
# the ones marked (?) are the least certain.
#
#    0  heart                    19  shamrock               38  frog
#    1  Uncle Sam hat            20  Santa                  39  candy cane
#    2  ball (?)                 21  blue gem               40  gift box
#    3  blue egg                 22  yellow scared face     41  maple leaf
#    4  gold star                23  face, red bow (?)      42  French flag
#    5  grey oval (?)            24  yellow burst           43  dollar sign
#    6  top hat                  25  dragon (?)             44  sugar skull
#    7  jester                   26  cake                   45  bonfire
#    8  snowflake                27  groundhog (?)          46  Scottish saltire
#    9  sun                      28  green tree             47  Australian flag
#   10  pale ball (?)            29  green-white-red flag   48  padlock
#   11  grey ball (?)            30  not seen               49  German flag
#   12  half moon, dark left     31  US flag                50  Greek flag
#   13  half moon, dark right    32  sailing ship           51  Italian flag
#   14  red-and-white ball (?)   33  roast turkey           52  crown
#   15  dark ball (?)            34  "JULY 4th"             53  bull
#   16  Lincoln portrait         35  TW (Tiger Woods logo)  54  Swedish flag
#   17  yellow sparkles          36  pie                    55  Swiss flag
#   18  jack-o'-lantern          37  EA logo
ICON_BALL = 2                    # an ordinary day, as in TW04: the ball
ICON_DEFAULT = ICON_BALL

# The year's special events: a date gets its own icon, and its name --
# "Halloween at Pebble Beach".  Real holidays where the art was made for one;
# invented events for the rest, so every icon we could read is used once.
# Left out: the balls and the red-bow face (5, 10, 11, 14, 15, 23), which we
# could not read well enough to name, and 30, never seen.
SPECIAL_DAYS = {
    # January
    (1, 1):   (17, "New Year's Day"),          # sparkles
    (1, 15):  (8,  'Winter Open'),             # snowflake
    (1, 26):  (47, 'Australia Day'),           # Australian flag
    # February
    (2, 2):   (27, 'Groundhog Day'),           # groundhog
    (2, 10):  (25, 'Dragon Cup'),              # dragon
    (2, 14):  (0,  "Valentine's Day"),         # heart
    (2, 22):  (16, "Presidents' Day"),         # Lincoln
    (2, 29):  (38, 'Leap Day Classic'),        # frog -- leap years only
    # March
    (3, 1):   (21, 'Diamond Invitational'),    # blue gem
    (3, 14):  (36, 'Pi Day Classic'),          # pie
    (3, 17):  (19, "St Patrick's Day"),        # shamrock
    (3, 20):  (12, 'Spring Equinox'),          # half moon
    (3, 25):  (50, 'Greek Independence Day'),  # Greek flag
    # April
    (4, 1):   (7,  "April Fools' Open"),       # jester
    (4, 10):  (48, 'Members Only Invitational'),  # padlock
    (4, 15):  (43, 'Tax Day Shootout'),        # dollar sign
    (4, 22):  (28, 'Earth Day'),               # green tree
    # May
    (5, 5):   (29, 'Cinco de Mayo'),           # green-white-red flag
    (5, 6):   (52, 'Coronation Cup'),          # crown
    (5, 28):  (37, 'EA Sports Cup'),           # EA logo -- EA, 28 May 1982
    # June
    (6, 2):   (51, 'Festa della Repubblica'),  # Italian flag
    (6, 6):   (54, 'Swedish National Day'),    # Swedish flag
    (6, 14):  (31, 'Flag Day'),                # US flag
    (6, 21):  (9,  'Midsummer Shootout'),      # sun
    # July
    (7, 1):   (41, 'Canada Day'),              # maple leaf
    (7, 4):   (34, 'Independence Day'),        # "JULY 4th"
    (7, 7):   (53, 'Running of the Bulls'),    # bull
    (7, 14):  (42, 'Bastille Day'),            # French flag
    # August
    (8, 1):   (55, 'Swiss National Day'),      # Swiss flag
    (8, 15):  (24, 'Summer Sizzler'),          # yellow burst
    # September
    (9, 13):  (1,  'Uncle Sam Day'),           # Uncle Sam hat
    (9, 22):  (13, 'Autumn Equinox'),          # the other half moon
    (9, 28):  (26, 'Anniversary Classic'),     # cake -- this server, 2026
    # October
    (10, 3):  (49, 'German Unity Day'),        # German flag
    (10, 12): (32, 'Columbus Day'),            # ship
    (10, 30): (22, 'Fright Night'),            # scared face
    (10, 31): (18, 'Halloween'),               # jack-o'-lantern
    # November
    (11, 2):  (44, 'Day of the Dead'),         # sugar skull
    (11, 5):  (45, 'Bonfire Night'),           # bonfire
    (11, 11): (4,  'Veterans Day'),            # gold star
    (11, 30): (46, "St Andrew's Day"),         # saltire
    # December
    (12, 24): (39, 'Christmas Eve'),           # candy cane
    (12, 25): (20, 'Christmas Day'),           # Santa
    (12, 26): (40, 'Boxing Day'),              # gift box
    (12, 30): (35, "Tiger's Birthday"),        # TW -- Tiger Woods, 30 Dec
    (12, 31): (6,  "New Year's Eve"),          # top hat
}


def easter(year):
    """Easter Sunday (the anonymous Gregorian computus)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def thanksgiving(year):
    """US Thanksgiving: the fourth Thursday of November."""
    first = datetime.date(year, 11, 1)
    return first + datetime.timedelta(days=(3 - first.weekday()) % 7 + 21)


# Holidays whose date moves, (function of the year, icon, name).  They take
# precedence over a fixed event on the same day -- Easter can land on April
# Fools' or Tax Day -- and keep their own icons, so none is ever shared.
MOVABLE_DAYS = (
    (easter, 3, 'Easter Sunday'),              # blue egg
    (thanksgiving, 33, 'Thanksgiving'),        # roast turkey
)

# The names this list replaced (TW04's, carried into the fork).  Stored
# months that still use one are renamed -- see lobbyd.rename_calendar.
OLD_SPECIAL_NAMES = (
    "New Year's", "Valentine's", "St Patrick's", 'Cinco de Mayo',
    'Independence Day', 'Halloween', 'Christmas Eve', 'Christmas',
    'Winter Open', 'Founders Cup', 'Red Ball Challenge', 'Anniversary Classic',
    'Eclipse Invitational', 'Birdie Bonanza', 'Derby Day', 'Diamond Jubilee',
    'All-Star Invitational', 'Midsummer Shootout', 'Sunrise Open',
    'Dog Days Open', 'Half Moon Classic', 'Pink Ribbon Classic',
    "Devil's Night", 'Wildcard Open',
)


def holiday(day):
    """(icon, name) for a day that has one, or None."""
    d = from_day(day)
    for when, icon, name in MOVABLE_DAYS:
        if when(d.year) == d:
            return icon, name
    return SPECIAL_DAYS.get((d.month, d.day))


def special_name(day, course_name):
    """What a special day is called: "Halloween at Pebble Beach" when it
    fits the 32-character field (the client truncates without telling
    anyone), else just "Halloween".  None on an ordinary day."""
    special = holiday(day)
    if not special:
        return None
    joined = '%s at %s' % (special[1], course_name) if course_name else special[1]
    return joined if len(joined) <= NAME_MAX else special[1]

# Offsets 4..11 and 15 are unnamed.  One of them almost certainly means "this
# event needs a password": the client prompted for one while they were all
# non-zero, and `0x002DE440` only appends the `PASS` tag once the player has
# typed something into the buffer at `0x003EEB50`, which starts empty.


def make_entry(name, day, purse=0, course=0, width=DATA_BYTES, data=None,
               icon=ICON_DEFAULT):
    """One calendar entry, in the fields that are known.

    Everything not named here stays zero.  That is deliberate rather than lazy:
    the unknown bytes were all non-zero in the first probe and the client then
    demanded a password to enter the event, so zero is the setting least likely
    to turn something on by accident.
    """
    out = bytearray(data or bytearray(width))
    out = out[:width].ljust(width, b'\0')
    if not 0 <= day <= DAY_MAX:
        raise ValueError('day %d is outside the 16 bits the client reads' % day)
    if width == DATA_BYTES:
        # TW05's 60-byte calendar entry.  `course` is an index into
        # twstats.COURSES and goes out as its four-character code; the event
        # name also goes in the headline text, and the icon in its own byte.
        # entries_for adds "no forced golfer" for real events.
        struct.pack_into('<I', out, PURSE_OFFSET, purse & 0xFFFFFFFF)
        struct.pack_into('<H', out, DAY_OFFSET, day)
        out[ICON_OFFSET_TW05] = icon & 0xFF
        if 0 <= course < len(COURSE_CODES):
            struct.pack_into('<I', out, COURSE_CODE_OFFSET,
                             course_code(COURSE_CODES[course]))
        if not any(out[TEXT_OFFSET:TEXT_OFFSET + TEXT_BYTES]):
            text = _ascii(name).encode('ascii')[:TEXT_BYTES - 1]
            out[TEXT_OFFSET:TEXT_OFFSET + len(text)] = text
        return (name, bytes(out))
    struct.pack_into('<H', out, DAY_OFFSET, day)
    if width > PURSE_OFFSET + 3:
        struct.pack_into('<I', out, PURSE_OFFSET, purse & 0xFFFFFFFF)
    if width > COURSE_OFFSET:
        out[COURSE_OFFSET] = course & 0xFF
    if width > ICON_OFFSET:
        out[ICON_OFFSET] = icon & 0xFF
    return (name, bytes(out))


# A rotation for the daily events.  A tournament wants a fixed, known layout
# that every entrant plays, which is the whole point of a daily leaderboard, so
# several entries in the course table are not eligible:
#
#   7   Skillz        not a golf course -- the skills-challenge venue, and it
#                     does not work online
#   21  NA            a placeholder
#   23  Random 18     a different layout for every player
#   24-29 Compilations  ditto
#
# 22 (Tiger's Dream 18) is kept: it is a fixed eighteen holes like any other.
#
# TW05: the table has 15 courses; 14 (Arcade Course) is not a real course.
UNPLAYABLE_COURSES = (14,)
TOURNEY_COURSES = tuple(c for c in range(15) if c not in UNPLAYABLE_COURSES)

# The tournament courses, HARDEST FIRST.  From course_difficulty.json (CPU
# versus CPU averages), in its order but for one correction: the file has Bay
# Hill Club first, and it is nearer ninth.  The rank alone sets the purse --
# the file's numbers do not quite follow its own order, and the order is what
# was reviewed.  Every course in TOURNEY_COURSES must be here; the self-test
# checks it.
#
# TW05: straight from course_difficulty.json's "tw05" averages, not yet
# reviewed by the operator the way TW04's order was.
DIFFICULTY_ORDER = (
    'Paradise Cove', 'Emerald Dragon', 'Greek Isles', 'TPC at Sawgrass',
    'Colonial CC', 'Pebble Beach', 'Turnberry-Ailsa', 'Harbour Town',
    'Edgewood GC', 'Fancourt Links', 'Sherwood CC', 'St Andrews',
    'Troon North Monument', "Coeur d'Alene",
)
PURSE_TOP = 5000000              # the hardest course
PURSE_BOTTOM = 2000000           # the easiest
PURSE_ROUND = 50000


def course_purse(course):
    """An event's purse from its course: $5,000,000 for the hardest, down in
    equal steps to $2,000,000 for the easiest, to the nearest $50,000.

    Event CONDITIONS (holes, tees, rough, fairways, greens) will weigh in too
    once the entry bytes that set them are mapped -- until then every event
    plays the game's defaults, so the course is the whole story.  A course not
    in the ranking gets the middle of the range rather than an error: a purse
    is not worth refusing to schedule an event over."""
    import twstats                       # twstats imports nothing from here
    name = twstats.course_name(course)
    if name not in DIFFICULTY_ORDER:
        return int(round((PURSE_TOP + PURSE_BOTTOM) / 2.0 / PURSE_ROUND)) * PURSE_ROUND
    rank = DIFFICULTY_ORDER.index(name)
    step = (PURSE_TOP - PURSE_BOTTOM) / float(len(DIFFICULTY_ORDER) - 1)
    return int(round((PURSE_TOP - rank * step) / PURSE_ROUND)) * PURSE_ROUND

EVENT_NAMES = ('Open', 'Classic', 'Invitational', 'Championship', 'Masters',
               'Challenge', 'Cup')


def name_for_day(day, course_name=None, rng=None):
    """A readable event name.

    The suffix is drawn from the month's own generator when there is one.
    Keying it off `day % 7` instead put the same word on the same course every
    time the rotation came round -- a 21-course pool in a 30-day month repeats
    nine courses, and seven days apart they came back identically named.
    """
    word = (rng.choice(EVENT_NAMES) if rng
            else EVENT_NAMES[day % len(EVENT_NAMES)])
    return ('%s %s' % (course_name, word))[:NAME_MAX] if course_name else word


def month_days(year, month):
    """(first day, length) for a calendar month, in day numbers."""
    first = datetime.date(year, month, 1)
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return to_day(first), (nxt - first).days


def month_of(day):
    """(year, month) a day falls in."""
    d = from_day(day)
    return d.year, d.month


def generate_month(year, month, courses=None):
    """A month of events: [{day, name, course, purse}], one per day.

    Seeded from the month itself rather than the clock.  That is deliberate:
    the calendar is stored once and served from storage afterwards, so an
    unseeded generator would be fine day to day -- but if the database were
    ever lost or rebuilt, every past event would come back different, and the
    rounds already recorded against them would be describing a course nobody
    played.  Seeding this way means a regenerated month is the same month.

    A course is not repeated until the pool runs low, so a month reads like a
    tour rather than a shuffle that lands on Pebble Beach three times in a week.
    """
    first, length = month_days(year, month)
    rng = random.Random('TW04 %04d-%02d' % (year, month))
    pool = []
    out = []
    taken = set()          # names already used this month
    for i in range(length):
        if not pool:
            pool = list(TOURNEY_COURSES)
            rng.shuffle(pool)
        course = pool.pop()
        cname = courses[course] if courses and course < len(courses) else None
        # The purse is the course's (course_purse).  The random draw that used
        # to set it is still made and thrown away: removing it would shift
        # every later draw, so a month regenerated from scratch would no longer
        # be the month that was stored -- different courses and names on days
        # people have already seen.
        rng.randrange(500000, 5000001, 50000)
        day = first + i
        conditions = event_conditions(day)
        purse = event_purse(course, conditions)
        # A date with an icon made for it gets that icon, and takes its name
        # from the day rather than the rotation -- "Christmas at Pebble Beach"
        # says more than "Pebble Beach Masters", and the icon on the calendar
        # is then explained by the name when the day is opened.
        special = holiday(day)
        icon = special[0] if special else ICON_DEFAULT
        # A month reuses nine courses -- 21 in the pool, 30 days -- so the
        # suffix has to avoid landing on the same pairing twice or the schedule
        # lists the same event name in the same month.
        for _ in range(len(EVENT_NAMES)):
            name = name_for_day(day, cname, rng)
            if name not in taken:
                break
        taken.add(name)
        if special:
            name = special_name(day, cname)
        out.append({'day': day, 'name': name[:NAME_MAX], 'course': course,
                    'purse': purse, 'icon': icon, 'conditions': conditions})
    return out


def replace_course(day, courses=None):
    """A playable event for a day whose course turned out not to be.

    Deterministic from the day, so repairing the same day twice gives the same
    answer, and independent of the month's own sequence -- the point is to
    change ONE day without disturbing the rest of a calendar that people may
    already have looked at or played.
    """
    rng = random.Random('TW04 repair %d' % day)
    course = rng.choice(TOURNEY_COURSES)
    cname = courses[course] if courses and course < len(courses) else None
    return {'day': day, 'course': course,
            'name': name_for_day(day, cname)}


def entries_for(events):
    """[{day, name, course, purse, icon}] -> entries, sorted as the client needs.

    `icon` is worked out from the date when an event does not carry one, so
    rows stored before that column existed still get their holiday.
    """
    out = []
    for e in sorted(events, key=lambda e: e['day']):
        icon = e.get('icon')
        if icon is None:
            special = holiday(e['day'])
            icon = special[0] if special else ICON_DEFAULT
        data = bytearray(DATA_BYTES)
        conditions = full_conditions(e.get('conditions'))
        struct.pack_into('<I', data, CONDITIONS_OFFSET,
                         conditions_word(conditions))
        struct.pack_into('<I', data, RULES_OFFSET,
                         conditions_word(conditions, RULES_OFFSET))
        data[FORCED_GOLFER_OFFSET] = NO_FORCED_GOLFER
        data[MAX_ATTEMPTS_OFFSET] = NO_ATTEMPT_LIMIT
        out.append(make_entry(e['name'], e['day'], e['purse'], e['course'],
                              data=data, icon=icon))
    return out


# The bytes still unnamed, in the order probe_bytes walks them.
UNKNOWN_OFFSETS = (4, 5, 6, 7, 8, 9, 10, 11, 15)


def probe_icons(count, start, courses=None):
    """One day per icon number, so a month draws every icon there is.

    TW05: numbered by the DATE, not by position in the request -- the client
    asks for overlapping ranges as it pages, and a number must not move.  An
    odd month runs 0..30 (the 1st is icon 0), an even month 31..61, so two
    screenshots cover 62 numbers and a cell's date says its number: odd month
    day d is d - 1, even month d + 30.  The headline says it too.
    """
    out = []
    for i in range(count):
        day = start + i
        d = from_day(day)
        icon = d.day - 1 + (31 if d.month % 2 == 0 else 0)
        course = TOURNEY_COURSES[i % len(TOURNEY_COURSES)]
        out.append(make_entry('Icon %d' % icon, day, 1000000, course,
                              icon=icon))
    return out


def probe_bytes(count, start, purse=1000000, course=14, after=None):
    """One day per unknown byte, that byte set to 1 and everything else zero.

    `probe_days` sets every byte at once, which names offsets but cannot say
    which byte drives which line -- and it turned every flag on, so the client
    demanded a password for every event.  This walks them one at a time:

        day 1   B04=1     only byte 4 is set
        day 2   B05=1     only byte 5
        ...
        day 10  CLEAN     nothing but purse, day and course

    Open CLEAN first to see the screen with every unknown byte at zero, then
    each B-day to see exactly what that one byte moved.  Whichever day brings
    the password prompt back is the "invitation only" flag.
    """
    after = today() if after is None else after
    entries, n = [], 0
    for i in range(count):
        day = start + i
        # A day in the past opens EVENT RESULTS, which draws none of these
        # fields, and only TODAY's event can be entered at all.  So the varied
        # entries go on days AFTER today, and today itself is left clean so it
        # can always be started.
        if day > after and n < len(UNKNOWN_OFFSETS):
            off = UNKNOWN_OFFSETS[n]
            data = bytearray(DATA_BYTES)
            data[off] = 1
            # No '=' in the name: it travels inside a TagField VALUE, and
            # the client's field getter splits on the first one it finds.
            name = 'BYTE%02d' % off
            n += 1
        else:
            data, name = bytearray(DATA_BYTES), 'CLEAN'
        entries.append(make_entry(name, day, purse, course, data=data))
    return entries


# THE EVENT CONDITIONS are one-hot flags in the little-endian word at data
# byte 8 (entry +0x28), read by one getter per setting at 0x002DFFD0..
# 0x002E0230 and drawn on UPCOMING EVENT from the text tables beside them
# (0x002FAD40 for holes, 0x00316EF0.. for the rest).  No flag is the default,
# which is what every event showed until now: All 18, Black, Average, Slow,
# Medium.  Order matters only when two flags of one setting are set -- the
# getter tests them in the order listed here, and the first wins.
#
#   holes     0x002DFFD0   bit 0 Front 9, bit 1 Back 9          (Par 3's etc. in
#                          the table are unreachable from here)
#   tees      0x002E0000   bit 3 White, bit 4 Blue              (Red unreachable)
#   rough     0x002E00E0   bit 13 Short, bit 15 Long
#   fairways  0x002E0030   bit 18 Fast, bit 17 Medium           (probe-confirmed:
#                          byte 10 = 2 or 3 drew Medium)
#   greens    0x002E00B0   bit 10 Slow, bit 12 Fast
#
# Three more are applied when an event starts (0x002DEB30) but never drawn,
# and nothing names them: bits 7-9 -> 0x00326E84, bits 19-25 -> 0x0032178A
# (default 4), bits 26-29 -> 0x0032178B.  Left at their defaults.  Bit 11 of
# the word at data byte 4 is tested alone at 0x002E0630 -- the likeliest
# "invitation only" flag (45.6).
CONDITIONS_OFFSET = 12           # TW05 (TW04: 8)
# TW05: the same idea, in the u32 at data byte 12, and every bit moved.  Read
# off the accessors TW05's event screen calls (0x001D6968 holes, 0x001D69B8
# tees, 0x001D6A58 rough, 0x001D69D8 fairways, 0x001D6A30 greens) and
# confirmed on screen with --probe-layout settings (2026-09-28) for tees
# White/Blue, rough Short/Long, fairways Medium and greens Fast.
CONDITIONS = {
    'holes':    {'All 18': 0, 'Front 9': 1 << 0, 'Back 9': 1 << 1},
    'tees':     {'Black': 0, 'White': 1 << 4, 'Blue': 1 << 5},
    'rough':    {'Average': 0, 'Short': 1 << 14, 'Long': 1 << 16},
    'fairways': {'Slow': 0, 'Medium': 1 << 18, 'Fast': 1 << 19},
    'greens':   {'Medium': 0, 'Slow': 1 << 11, 'Fast': 1 << 13},
    # These two live in the u32 at data byte 8 (RULES_OFFSET), not 12.  Read
    # off 0x001D6B78 and 0x001D6990 and confirmed on the UPCOMING EVENT screen
    # 2026-09-30 (--probe-layout settings).
    #
    # ATTRIBUTES: every golfer's ratings set to one level for the round
    # (0x001D6FF0 turns the option into 50..100, stored when the event
    # starts).  No bit at all is "All 100's", which is what every event had
    # before this was mapped; 0x20000 draws N/A -- no cap, the player's own
    # golfer as they built them.
    'attributes': {"All 100's": 0, "All 90's": 1 << 22, "All 80's": 1 << 21,
                   "All 70's": 1 << 20, "All 60's": 1 << 19,
                   "All 50's": 1 << 18, 'Own': 1 << 17},
    # DIFFICULTY: Tour switches on the game's Tour difficulty when the event
    # starts (0x00170EA0(1)); Default leaves it off.  No bit draws N/A and
    # plays the same as Default, so events always say which.
    'difficulty': {'Default': 1 << 15, 'Tour': 1 << 16},
}
RULES_OFFSET = 8                 # the u32 holding attributes and difficulty
SETTING_OFFSETS = {'attributes': RULES_OFFSET, 'difficulty': RULES_OFFSET}
DEFAULT_CONDITIONS = {k: next(n for n, bit in v.items() if not bit)
                      for k, v in CONDITIONS.items() if k != 'difficulty'}
DEFAULT_CONDITIONS['difficulty'] = 'Default'
# Also in the data-8 word, and deliberately left clear: bits 7, 8 and 11 only
# pick the calendar's status line for a day (0x0020AC20, through 0x001D6FE0),
# and bits 8-10 of the data-12 word are applied at the start (0x001D6A00) but
# never drawn -- probably the pins.
#
# MAX ATTEMPTS (data byte 6) is only drawn, never enforced (0x0020AFBC,
# 0x0020B870); 100 or more draws N/A, which is the truth -- the server takes
# any number of rounds and keeps the best.
MAX_ATTEMPTS_OFFSET = 6
NO_ATTEMPT_LIMIT = 100
INVITATION_FLAG = (4, 1 << 11)       # (data byte of the word, bit) -- unconfirmed


def conditions_word(conditions, offset=CONDITIONS_OFFSET):
    """{'tees': 'White', 'greens': 'Fast', ...} -> the u32 for data byte
    `offset`: 12 for the course conditions, RULES_OFFSET (8) for attributes
    and difficulty.  Anything not named is the default; an unknown value is
    an error, because a typo would otherwise quietly become the default."""
    word = 0
    for setting, value in (conditions or {}).items():
        bit = CONDITIONS[setting][value]
        if SETTING_OFFSETS.get(setting, CONDITIONS_OFFSET) == offset:
            word |= bit
    return word


# The settings an Online Tournament event varies.  Holes is not one of them:
# a tournament round is always All 18 (the default, no flag), so its flags are
# documented above but never set.  Attributes and difficulty come last, so the
# draws for the first four (event_conditions) are the ones they always were.
EVENT_SETTINGS = ('tees', 'rough', 'fairways', 'greens', 'attributes',
                  'difficulty')

# What each option does to an event's purse, on top of its course purse.
# Agreed with the operator; the harder the setting, the bigger the prize.  The
# defaults are not all 1.00 -- Black tees are the hardest there are, and the
# game's own default -- so an event on untouched settings pays a little over
# its course purse.
PURSE_MULTIPLIERS = {
    'tees':     {'Black': 1.10, 'Blue': 1.00, 'White': 0.90},
    'rough':    {'Short': 0.95, 'Average': 1.00, 'Long': 1.10},
    'fairways': {'Slow': 0.97, 'Medium': 1.00, 'Fast': 1.05},
    'greens':   {'Slow': 0.95, 'Medium': 1.00, 'Fast': 1.10},
    # Lower ratings make every shot harder, so they pay more; a player's own
    # golfer is as good as they have made it, so it pays as All 100's does.
    'attributes': {"All 100's": 1.00, 'Own': 1.00, "All 90's": 1.02,
                   "All 80's": 1.05, "All 70's": 1.08, "All 60's": 1.12,
                   "All 50's": 1.15},
    'difficulty': {'Default': 1.00, 'Tour': 1.10},
}
# How often an event keeps each setting's default; the rest is split evenly
# between the other options.  Half and half keeps most days recognisable while
# still giving a hard day now and then.
CONDITION_DEFAULT_CHANCE = 0.5
# Settings drawn by weight instead.  Most events stay a level field (All
# 100's) on Default; the low ratings are rare, so an All 50's day is an
# occasion rather than a chore.
CONDITION_WEIGHTS = {
    'attributes': {"All 100's": 50, 'Own': 20, "All 90's": 10, "All 80's": 10,
                   "All 70's": 5, "All 60's": 3, "All 50's": 2},
    'difficulty': {'Default': 70, 'Tour': 30},
}


def event_conditions(day):
    """The conditions for the event on `day`: {setting: option} for each of
    EVENT_SETTINGS.  Seeded from the day alone, NOT from the month's generator
    -- adding this must not shift a single course or name in a calendar that
    is already stored -- so the same day always gets the same conditions."""
    rng = random.Random('TW04 conditions %d' % day)
    out = {}
    for setting in EVENT_SETTINGS:
        if setting in CONDITION_WEIGHTS:
            weights = CONDITION_WEIGHTS[setting]
            options = list(weights)
            out[setting] = rng.choices(options, [weights[o] for o in options])[0]
            continue
        default = DEFAULT_CONDITIONS[setting]
        others = sorted(o for o in CONDITIONS[setting] if o != default)
        out[setting] = (default if rng.random() < CONDITION_DEFAULT_CHANCE
                        else rng.choice(others))
    return out


def full_conditions(conditions):
    """Every EVENT_SETTING named, defaults filled in -- so a stored event with
    no conditions (one made before they existed) reads as what it was."""
    out = {s: DEFAULT_CONDITIONS[s] for s in EVENT_SETTINGS}
    out.update({k: v for k, v in (conditions or {}).items() if k in out})
    return out


def event_purse(course, conditions=None):
    """course_purse, times each setting's multiplier, to the nearest $50,000."""
    factor = 1.0
    for setting, option in full_conditions(conditions).items():
        factor *= PURSE_MULTIPLIERS[setting][option]
    return int(round(course_purse(course) * factor / PURSE_ROUND)) * PURSE_ROUND


SETTING_LABELS = {'tees': 'Tees', 'rough': 'Rough', 'fairways': 'Fairways',
                  'greens': 'Greens', 'attributes': 'Attributes',
                  'difficulty': 'Difficulty'}


def condition_items(conditions):
    """[(label, option, is_default)] for every EVENT_SETTING, in the order the
    UPCOMING EVENT screen draws them -- for showing ALL of an event's settings,
    not just the ones that differ from the game's defaults."""
    full = full_conditions(conditions)
    return [(SETTING_LABELS[s], full[s], full[s] == DEFAULT_CONDITIONS[s])
            for s in EVENT_SETTINGS]


def describe_conditions(conditions):
    """For people: the settings that differ from the game's defaults, or
    'Standard conditions'.  'Long rough, fast greens'."""
    words = {'tees': '%s tees', 'rough': '%s rough', 'fairways': '%s fairways',
             'greens': '%s greens', 'attributes': '%s attributes',
             'difficulty': '%s difficulty'}
    parts = [words[s] % o.lower()
             for s, o in full_conditions(conditions).items()
             if o != DEFAULT_CONDITIONS[s]]
    if not parts:
        return 'Standard conditions'
    text = ', '.join(parts)
    return text[0].upper() + text[1:]


# The confirmation calendar: one setting per day, each NAMED for what
# UPCOMING EVENT should then draw, so checking is "does the line match the
# name".  Built from CONDITIONS, so it checks the very table events will use.
PROBE_PLAN = (
    [('BASELINE', {})]
    + [('%s %s' % (s.upper(), v.upper()), {s: v})
       for s in EVENT_SETTINGS for v, bit in CONDITIONS[s].items() if bit]
    + [('ALL HARD', {'tees': 'Black', 'rough': 'Long', 'fairways': 'Fast',
                     'greens': 'Fast'}),
       ('INVITE FLAG', 'invite')]
)


def probe_condition_for(day, anchor):
    """(name, conditions) the confirmation calendar puts on `day`, or None
    for an ordinary clean day.  A pure function of the day, so every request
    -- the calendar asks a month at a time -- sees the same layout: today is
    clean and playable, the plan starts tomorrow."""
    index = day - anchor - 1
    if 0 <= index < len(PROBE_PLAN):
        return PROBE_PLAN[index]
    return None


def probe_conditions(count, start, anchor=None, purse=1000000, course=0):
    """The confirmation calendar (PROBE_PLAN) for `count` days from `start`."""
    anchor = today() if anchor is None else anchor
    out = []
    for day in range(start, start + count):
        data = bytearray(DATA_BYTES)
        probe = probe_condition_for(day, anchor)
        if probe is None:
            name = 'CLEAN'
        else:
            name, conds = probe
            if conds == 'invite':
                where, bit = INVITATION_FLAG
                struct.pack_into('<I', data, where, bit)
            else:
                struct.pack_into('<I', data, CONDITIONS_OFFSET,
                                 conditions_word(conds))
        out.append(make_entry(name, day, purse, course, data=data))
    return out


# TW05: the event screen's headline prints data bytes 28..59 as TEXT -- under
# probe_days it drew four boxes, a space and !"#$%...; (0x1C..0x3B), then ran
# on into the next entry's name.  So 32 bytes of description from 28.
TEXT_OFFSET = 28
TEXT_BYTES = 32

# TW05 bytes not yet named, walked by probe_walk: everything but the purse
# (0..3), the day (16..17) and the text (28..59).
WALK_OFFSETS = tuple(range(4, 16)) + tuple(range(18, 28))


def probe_walk(count, start, after=None):
    """TW05: one day per unknown byte, set to 1, the rest zero.

    Today (`after`) is CLEAN -- only purse, day and a label -- so it can
    still be opened and entered.  Each later day sets exactly one byte from
    WALK_OFFSETS, and its description says which, so the UPCOMING EVENT
    screen names the byte behind whatever line moved.
    """
    after = today() if after is None else after
    out, n = [], 0
    for i in range(count):
        day = start + i
        data = bytearray(DATA_BYTES)
        label = 'CLEAN'
        if day > after and n < len(WALK_OFFSETS):
            data[WALK_OFFSETS[n]] = 1
            label = 'BYTE %02d = 1' % WALK_OFFSETS[n]
            n += 1
        text = label.encode('ascii')[:TEXT_BYTES - 1]
        data[TEXT_OFFSET:TEXT_OFFSET + len(text)] = text
        # Packed here, not through make_entry: that writes TW04's course and
        # icon bytes (14, 15), which would wipe the walk on those two days.
        struct.pack_into('<I', data, 0, 1000000)
        struct.pack_into('<H', data, DAY_OFFSET, day)
        out.append((label.replace(' = 1', ''), bytes(data)))
    return out


# TW05's course is a four-character code, stored as the little-endian u32 of
# the code read as a big-endian number: 'PEBB' is 0x50454242, bytes BBEP.
# 0x00132320 maps COM1..COMA and DR18 itself; everything else is looked up in
# the course table built from the disc at run time (0x003832E8, 0x58 a
# record, code first).  Read live, 2026-09-28:
COURSE_CODES = ('PEBB', 'SAIN', 'SAWG', 'JAPA', 'GREE', 'CARI', 'COLO', 'COEU',
                'SHER', 'HARB', 'FANC', 'TURN', 'TROO', 'EDGE', 'ARCA')
COURSE_CODE_OFFSET = 20          # u32, TW05 accessor 0x001D6910
TP_OFFSET = 18                   # u8, non-zero appends " (TP)"; 0x001D6960
# The FORCED GOLFER line reads data byte 7 (0x001D6930); 42 and 43 come back
# as -1, the "no forced golfer" value.  0 drew TIGER WOODS.  UNCONFIRMED on
# screen: the probe day for it was not reached.
FORCED_GOLFER_OFFSET = 7
NO_FORCED_GOLFER = 42


def course_code(code):
    return struct.unpack('<I', code.encode('ascii')[::-1])[0]


# One test per upcoming day, for probe_settings: (label, [(offset, kind,
# value)]) where kind is 'or32' (OR into a u32) or 'u8'.  Every day is on
# Pebble Beach so the course line has something to show.
SETTING_TESTS = (
    # data 8, 0x001D6990 (event row 1): 0x8000 -> 1, 0x10000 -> 2, else 0
    ('D8 0x8000', [(8, 'or32', 0x8000)]),
    ('D8 0x10000', [(8, 'or32', 0x10000)]),
    # data 8, 0x001D6B78 (event row 8): 0x20000..0x400000 -> 0..5, else 6
    ('D8 0x20000', [(8, 'or32', 0x20000)]),
    ('D8 0x40000', [(8, 'or32', 0x40000)]),
    ('D8 0x80000', [(8, 'or32', 0x80000)]),
    ('D8 0x100000', [(8, 'or32', 0x100000)]),
    ('D8 0x200000', [(8, 'or32', 0x200000)]),
    ('D8 0x400000', [(8, 'or32', 0x400000)]),
    # data 12, 0x001D6A00 (event row 2): 0x100 -> 1, 0x200 -> 2, 0x400 -> 3
    ('D12 0x100', [(12, 'or32', 0x100)]),
    ('D12 0x200', [(12, 'or32', 0x200)]),
    ('D12 0x400', [(12, 'or32', 0x400)]),
    # data 8 bit 11, 0x001D6FE0, read by the calendar's day callback
    ('D8 0x800', [(8, 'or32', 0x800)]),
    ('MAXATT 3', [(6, 'u8', 3)]),
    ('GOLFER 42', [(7, 'u8', 42)]),
)


def probe_settings(count, start, after=None):
    """TW05: check the inferred setting values on screen.  Today is clean;
    the Nth day after today carries SETTING_TESTS[N-1], by day, so the
    numbering holds across the month requests."""
    after = today() if after is None else after
    out = []
    for i in range(count):
        day = start + i
        data = bytearray(DATA_BYTES)
        struct.pack_into('<I', data, 0, 1000000)
        struct.pack_into('<H', data, DAY_OFFSET, day)
        struct.pack_into('<I', data, COURSE_CODE_OFFSET, course_code('PEBB'))
        label = 'CLEAN'
        k = day - after - 1
        if 0 <= k < len(SETTING_TESTS):
            label, writes = SETTING_TESTS[k]
            for off, kind, value in writes:
                if kind == 'u8':
                    data[off] = value
                else:
                    struct.pack_into('<I', data, off, struct.unpack_from(
                        '<I', data, off)[0] | value)
        text = label.encode('ascii')[:TEXT_BYTES - 1]
        data[TEXT_OFFSET:TEXT_OFFSET + len(text)] = text
        out.append((label, bytes(data)))
    return out


def probe_days(count, start):
    """Reachable entries whose OTHER bytes hold their own offsets.

    The day goes where the client looks for it, so the calendar can actually
    light the cell up and a day can be opened; every other byte carries its own
    offset, so whatever the round-details screen draws, it draws the offset of
    the field behind it.  Same trick as `--probe-stats`, now that the entries
    can be reached at all.
    """
    # The purse and course bytes are part of the pattern too, so they are
    # passed back in rather than left to make_entry's zero defaults -- 0x03020100
    # is bytes 0..3 holding their own offsets, and 14 is the byte at 14.
    # TW05: built directly, so only the day is placed -- make_entry would
    # also write a course code over bytes 20..23.
    out = []
    for i in range(count):
        data = bytearray(range(DATA_BYTES))
        struct.pack_into('<H', data, DAY_OFFSET, start + i)
        out.append(('DAY%02d' % i, bytes(data)))
    return out


# The error codes the client has messages for, from the table at 0x003029C0.
# They are stored little-endian, so they read backwards in the image: 'ssap' is
# 'pass'.  These go in the frame header's SECOND WORD, the way `fail()` sends
# its own -- and unlike ours, the client has real text for every one.
ERRORS = {
    'miss': 'Required field missing',
    'auth': 'Authorization error',
    'user': 'User is invalid',
    'pass': 'Password is invalid',
    'hack': 'Attempted password hacking',
    'rsrc': 'Resource is invalid',
    'netw': 'Network problem',
    'bsod': 'Internal error',
}


def probe(count=1, start=0, width=DATA_BYTES):
    """One entry per candidate offset for the date field.

    Only needed for the RESULTS list now.  The info list's day offset is known
    -- see DAY_OFFSET -- so `probe_days` is the better probe there; the results
    list is 12 bytes wide and its own day offset has not been found yet.

    Entry `k` is the day `start + k`, written as a 32-bit little-endian number
    at byte offset `k` and nothing else.  So if the calendar lights up the day
    `start + 4`, the date lives at `+0x04`.  One screenshot, no ambiguity.

    A 32-bit write also satisfies a 16-bit reader at the same offset, because
    the low half sits first and a day number is comfortably under 65536 until
    the year 2079 -- so this finds the offset whichever width the field is.

    `start` is a day number, so the entries land on real days on the calendar
    rather than at list positions.
    """
    entries = []
    for k in range(min(count, width - 3)):
        data = bytearray(width)
        struct.pack_into('<I', data, k, start + k)
        entries.append(('OFF%02d' % k, bytes(data)))
    return entries


EMPTY = encode_bytes([0])        # a valid list with nothing in it


def main():
    fails = []
    print('entry = %d bytes, %d of data, at most %d entries'
          % (ENTRY_SIZE, DATA_BYTES, MAX_ENTRIES))

    # 1. the reader stops where the client's does
    data, used = decode_bytes('41424344\n4546', 8)
    if data != b'ABCD' or used != 8:
        fails.append('a newline should end the run, got %r after %d' % (data, used))
    data, _ = decode_bytes('FF00ff00', 4)
    if data != b'\xff\x00\xff\x00':
        fails.append('lower case should decode too, got %r' % data)

    # 2. a list round-trips, including the literal name
    entries = [('Pebble Beach', bytes(bytearray(range(DATA_BYTES)))),
               ('X', b'\xff' * DATA_BYTES)]
    body = encode_list(entries)
    print('two entries encode to %d characters: %s...' % (len(body), body[:40]))
    if decode_list(body) != entries:
        fails.append('a list did not round-trip')
    if 'Pebble Beach' not in body:
        fails.append('the name should appear literally in the body')

    # 3. the empty list is two characters, not a TagField
    if decode_list(EMPTY) != [] or EMPTY != '00':
        fails.append('the empty list should be "00", got %r' % EMPTY)

    # 4. what lobbyd had been sending instead
    junk, _ = decode_bytes('COUNT=0', 1)
    print('the old TagField reply "COUNT=0" decodes to a count of %d'
          % bytearray(junk)[0])
    if bytearray(junk)[0] <= MAX_ENTRIES:
        fails.append('"COUNT=0" was supposed to overrun the array')

    # 5. the epoch, against the one request the calendar actually made
    if from_day(46266) != datetime.date(2026, 9, 1):
        fails.append('START=46266 should be 2026-09-01, got %s' % from_day(46266))

    # 6. the probe puts day start+k at offset k and leaves the rest alone
    day = to_day(datetime.date(2026, 9, 1))
    entries = probe(30, day)
    print('probe: %d entries, %s carries %s at +0x%02X'
          % (len(entries), entries[4][0], from_day(day + 4), 4))
    for k, (name, data) in enumerate(entries):
        want = bytearray(DATA_BYTES)
        struct.pack_into('<I', want, k, day + k)
        if name != 'OFF%02d' % k or data != bytes(want):
            fails.append('probe entry %d is wrong: %r %r' % (k, name, data))
    if len(entries) != min(30, DATA_BYTES - 3):
        fails.append('a 32-bit write fits at every offset but the last 3, '
                     'up to the 30 asked for, not %d'
                     % len(entries))

    # 7. a day lands where 0x002DF03C reads it, and the list comes out sorted
    days = probe_days(5, day)
    for i, (name, data) in enumerate(days):
        got = struct.unpack_from('<H', data, DAY_OFFSET)[0]
        if got != day + i or name != 'DAY%02d' % i:
            fails.append('entry %d carries day %d as %d' % (i, day + i, got))
        for k in range(DATA_BYTES):
            if k not in (DAY_OFFSET, DAY_OFFSET + 1) and bytearray(data)[k] != k:
                fails.append('byte %d should name itself, got %d'
                             % (k, bytearray(data)[k]))
    order = [struct.unpack_from('<H', d, DAY_OFFSET)[0] for _, d in days]
    if order != sorted(order):
        fails.append('the list must be sorted ascending: 0x002DF03C range-checks '
                     'the first and last entry before it scans')
    print('day probe: %s is %s, day at +0x%02X' % (days[0][0], from_day(day),
                                                   DAY_OFFSET))

    # 7b. a generated month: known fields set, everything else left alone
    year, mon = month_of(day)
    first, length = month_days(year, mon)
    events = generate_month(year, mon)
    if len(events) != length:
        fails.append('%04d-%02d has %d days, generated %d'
                     % (year, mon, length, len(events)))
    if [e['day'] for e in events] != list(range(first, first + length)):
        fails.append('a month must be one event a day, in order')
    if generate_month(year, mon) != events:
        fails.append('regenerating a month must give the same month back')
    if generate_month(year, mon) == generate_month(year + 1, mon):
        fails.append('two different months should not generate the same events')

    cal = entries_for(events)
    for i, (name, data) in enumerate(cal):
        b_ = bytearray(data)
        ev = events[i]
        if struct.unpack_from('<I', data, PURSE_OFFSET)[0] != ev['purse']:
            fails.append('entry %d lost the purse' % i)
        if struct.unpack_from('<I', data, COURSE_CODE_OFFSET)[0] != \
                course_code(COURSE_CODES[ev['course']]):
            fails.append('entry %d lost the course' % i)
        if struct.unpack_from('<H', data, DAY_OFFSET)[0] != ev['day']:
            fails.append('entry %d lost the day' % i)
        # Nothing still unknown may be set: bytes 4..7 hold the likely
        # "invitation only" flag, which turns on a password prompt.  Bytes
        # 8..11 are the conditions word (CONDITIONS) and may carry ONLY the
        # flags in CONDITIONS -- never one of the three unnamed settings.
        # TW05: purse 0-3, max attempts 6, forced golfer 7, attributes and
        # difficulty 8-11, conditions 12-15, day 16-17, course code 20-23 and
        # the headline text 28-59.
        spare = [k for k in range(DATA_BYTES)
                 if k not in (0, 1, 2, 3, MAX_ATTEMPTS_OFFSET,
                              FORCED_GOLFER_OFFSET, DAY_OFFSET,
                              DAY_OFFSET + 1, ICON_OFFSET_TW05)
                 and not RULES_OFFSET <= k < RULES_OFFSET + 4
                 and not CONDITIONS_OFFSET <= k < CONDITIONS_OFFSET + 4
                 and not COURSE_CODE_OFFSET <= k < COURSE_CODE_OFFSET + 4
                 and not TEXT_OFFSET <= k < TEXT_OFFSET + TEXT_BYTES
                 and b_[k]]
        if spare:
            fails.append('entry %d set unknown bytes %s' % (i, spare))
        known = 0
        for setting, flags in CONDITIONS.items():
            if setting not in SETTING_OFFSETS:
                for bit in flags.values():
                    known |= bit
        word = struct.unpack_from('<I', data, CONDITIONS_OFFSET)[0]
        rules = struct.unpack_from('<I', data, RULES_OFFSET)[0]
        if rules != conditions_word(full_conditions(ev['conditions']),
                                    RULES_OFFSET) or not rules:
            fails.append('entry %d: attributes and difficulty did not reach '
                         'data 8 (0x%08x)' % (i, rules))
        if data[MAX_ATTEMPTS_OFFSET] < 100:
            fails.append('entry %d draws a max attempts the server does not '
                         'enforce' % i)
        if word & ~known:
            fails.append('entry %d set condition bits nothing names: 0x%08x'
                         % (i, word & ~known))
    # One day taken on its own must be the same event as that day in the month,
    # or a player would start one event and be scored in another.
    mine = [e for e in events if e['day'] == day]
    if entries_for(mine) != [cal[day - first]]:
        fails.append('one day does not match its place in the month')
    if any(e['purse'] % 50000 for e in events):
        fails.append('purses should be round numbers')

    # 7a1. the confirmation calendar: the same layout however the calendar is
    #      asked, each day carrying exactly the flags its name promises
    anchor = to_day(datetime.date(2026, 9, 24))
    got = {}
    for first, n in ((to_day(datetime.date(2026, 9, 1)), 30),
                     (to_day(datetime.date(2026, 10, 1)), 31)):
        for name, data in probe_conditions(n, first, anchor):
            day = struct.unpack_from('<H', data, DAY_OFFSET)[0]
            got[day] = (name, struct.unpack_from('<I', data, CONDITIONS_OFFSET)[0],
                        struct.unpack_from('<I', data, INVITATION_FLAG[0])[0])
    planned = [got.get(anchor + 1 + i) for i in range(len(PROBE_PLAN))]
    for (name, conds), seen in zip(PROBE_PLAN, planned):
        want = ((name, 0, INVITATION_FLAG[1]) if conds == 'invite'
                else (name, conditions_word(conds), 0))
        if seen != want:
            fails.append('probe day %r carries %r, expected %r' % (name, seen, want))
    if got.get(anchor, ('',))[0] != 'CLEAN' or got.get(anchor + len(PROBE_PLAN) + 1,
                                                      ('',))[0] != 'CLEAN':
        fails.append('today and the days after the plan must be clean')
    # TW05, as --probe-layout settings drew them (2026-09-28).
    for setting, option, bit in (('tees', 'White', 0x10), ('tees', 'Blue', 0x20),
                                 ('rough', 'Short', 0x4000),
                                 ('rough', 'Long', 0x10000),
                                 ('fairways', 'Medium', 0x40000),
                                 ('greens', 'Fast', 0x2000)):
        if conditions_word({setting: option}) != bit:
            fails.append('%s %s must be 0x%x, as the screen drew'
                         % (setting, option, bit))
    for setting, option, bit in (('attributes', "All 50's", 0x40000),
                                 ('attributes', 'Own', 0x20000),
                                 ('difficulty', 'Default', 0x8000),
                                 ('difficulty', 'Tour', 0x10000)):
        if conditions_word({setting: option}, RULES_OFFSET) != bit or \
                conditions_word({setting: option}):
            fails.append('%s %s must be 0x%x in the data-8 word only, as the '
                         'screen drew' % (setting, option, bit))
    try:
        conditions_word({'tees': 'Red'})
        fails.append('an option the client cannot reach must be refused')
    except KeyError:
        pass
    print('probe:   %d confirmation days from %s'
          % (len(PROBE_PLAN), from_day(anchor + 1)))

    # 7a2. purses follow course difficulty
    import twstats
    unranked = [twstats.course_name(c) for c in TOURNEY_COURSES
                if twstats.course_name(c) not in DIFFICULTY_ORDER]
    if unranked:
        fails.append('tournament courses missing from DIFFICULTY_ORDER: %r'
                     % unranked)
    ladder = [course_purse(twstats.COURSES.index(n)) for n in DIFFICULTY_ORDER]
    if ladder[0] != PURSE_TOP or ladder[-1] != PURSE_BOTTOM:
        fails.append('the hardest should pay %d and the easiest %d, got %d / %d'
                     % (PURSE_TOP, PURSE_BOTTOM, ladder[0], ladder[-1]))
    if any(a <= b for a, b in zip(ladder, ladder[1:])):
        fails.append('a harder course must always pay more: %r' % ladder)
    wrong = [e for e in events
             if e['purse'] != event_purse(e['course'], e['conditions'])]
    if wrong:
        fails.append('%d generated events do not carry their course and '
                     'conditions purse' % len(wrong))

    # 7a3. conditions: valid, stable per day, on the wire, and priced
    for e in events:
        c = e['conditions']
        if set(c) != set(EVENT_SETTINGS) or any(
                c[s] not in CONDITIONS[s] for s in c):
            fails.append('bad conditions on day %d: %r' % (e['day'], c))
        if event_conditions(e['day']) != c:
            fails.append('conditions must be the same every time for a day')
    for (name, data), e in zip(entries_for(events), sorted(
            events, key=lambda e: e['day'])):
        if struct.unpack_from('<I', data, CONDITIONS_OFFSET)[0] != \
                conditions_word(e['conditions']):
            fails.append('%s: the conditions did not reach the entry' % name)
            break
    hard = {'tees': 'Black', 'rough': 'Long', 'fairways': 'Fast', 'greens': 'Fast'}
    easy = {'tees': 'White', 'rough': 'Short', 'fairways': 'Slow', 'greens': 'Slow'}
    top = twstats.COURSES.index(DIFFICULTY_ORDER[0])
    if not event_purse(top, easy) < course_purse(top) < event_purse(top, hard):
        fails.append('harder conditions must pay more than easier ones')
    if describe_conditions({}) != 'Standard conditions' or \
            describe_conditions({'rough': 'Long', 'greens': 'Fast'}) != \
            'Long rough, fast greens':
        fails.append('describe_conditions reads wrong: %r'
                     % describe_conditions({'rough': 'Long', 'greens': 'Fast'}))
    days = [event_conditions(d) for d in range(46000, 46365)]
    share = sum(1 for c in days if c['greens'] == 'Medium') / float(len(days))
    if not 0.4 < share < 0.6:
        fails.append('a setting should keep its default about half the time, '
                     'got %.2f' % share)
    for setting, weights in CONDITION_WEIGHTS.items():
        total = float(sum(weights.values()))
        for option, weight in weights.items():
            share = sum(1 for c in days if c[setting] == option) / float(len(days))
            if abs(share - weight / total) > 0.08:
                fails.append('%s %s drawn %.2f of days, weighted %.2f'
                             % (setting, option, share, weight / total))
    print('conditions: e.g. %s -> $%s'
          % (describe_conditions(events[0]['conditions']),
             format(events[0]['purse'], ',')))
    print('purses: %s $%s ... %s $%s'
          % (DIFFICULTY_ORDER[0], format(ladder[0], ','),
             DIFFICULTY_ORDER[-1], format(ladder[-1], ',')))
    print('calendar: %04d-%02d, %d events, first %r at $%s'
          % (year, mon, len(events), events[0]['name'],
             format(events[0]['purse'], ',')))

    # 7b1. holidays get their icon and their name, and names fit the field.
    # 2028 is a leap year, so Leap Day is there too; and the movable days
    # (Easter 16 April, Thanksgiving 23 November) land on no fixed one.
    checks = [(datetime.date(2028, mm, dd), icon, label)
              for (mm, dd), (icon, label) in SPECIAL_DAYS.items()]
    checks += [(when(2028), icon, label) for when, icon, label in MOVABLE_DAYS]
    for date, icon, label in checks:
        when = to_day(date)
        ev = [e for e in generate_month(2028, date.month) if e['day'] == when]
        if not ev:
            fails.append('%s is not in the generated month' % label)
            continue
        if ev[0]['icon'] != icon:
            fails.append('%s should draw icon %d, got %d'
                         % (label, icon, ev[0]['icon']))
        if not ev[0]['name'].startswith(label):
            fails.append('%s should be named for the day, got %r'
                         % (label, ev[0]['name']))
    if len(SPECIAL_DAYS[2, 29][1]) > NAME_MAX:
        fails.append('a special name is longer than the field')
    # the movable days fall where they should
    for got, want in ((easter(2026), datetime.date(2026, 4, 5)),
                      (easter(2027), datetime.date(2027, 3, 28)),
                      (thanksgiving(2026), datetime.date(2026, 11, 26)),
                      (thanksgiving(2027), datetime.date(2027, 11, 25))):
        if got != want:
            fails.append('movable holiday on %s, expected %s' % (got, want))
    over = [e['name'] for yy in (2026, 2027) for mm in range(1, 13)
            for e in generate_month(yy, mm) if len(e['name']) > NAME_MAX]
    if over:
        fails.append('%d name(s) longer than the %d-character field: %r'
                     % (len(over), NAME_MAX, over[:2]))
    # an ordinary day is a golf ball, not a heart
    plain = [e for e in generate_month(2027, 6) if not holiday(e['day'])]
    if any(e['icon'] != ICON_DEFAULT for e in plain):
        fails.append('an ordinary day should carry the default icon')
    # No two special days may share an icon -- the whole point is that each
    # one is recognisable on the calendar.
    icons = ([icon for icon, _ in SPECIAL_DAYS.values()]
             + [icon for _, icon, _ in MOVABLE_DAYS])
    if len(set(icons)) != len(icons):
        dupes = sorted(i for i in set(icons) if icons.count(i) > 1)
        fails.append('icons used by more than one day: %s' % dupes)
    if ICON_DEFAULT in icons:
        fails.append('a special day is using the everyday icon')
    print('icons:   %d special days a year, %d distinct icons, the rest a golf '
          'ball (%d)' % (len(icons), len(set(icons)), ICON_DEFAULT))

    # 7b2. nothing unplayable ever reaches a calendar
    for c in UNPLAYABLE_COURSES:
        if c in TOURNEY_COURSES:
            fails.append('course %d is unplayable and still in the rotation' % c)
    unplayable = 0
    for yy in (2026, 2027):
        for mm in range(1, 13):
            unplayable += sum(1 for e in generate_month(yy, mm)
                              if e['course'] in UNPLAYABLE_COURSES)
    if unplayable:
        fails.append('%d unplayable day(s) in two years of calendars' % unplayable)
    # and a repaired day lands somewhere playable, the same way every time
    fix = replace_course(day)
    if fix['course'] in UNPLAYABLE_COURSES:
        fails.append('a repair chose an unplayable course')
    if replace_course(day) != fix:
        fails.append('repairing the same day twice should give the same answer')
    print('courses: %d playable, %d excluded (%s), 24 months clean'
          % (len(TOURNEY_COURSES), len(UNPLAYABLE_COURSES),
             ' '.join(str(c) for c in UNPLAYABLE_COURSES)))

    # 7c. a result entry: the winner's name, the viewer's scores and finish
    r_name, r_data = make_result('JeddyH2', day, winner_score=67,
                                 your_score=72, place=2)
    if r_name != 'JeddyH2':
        fails.append('the result name should be the winner')
    if struct.unpack_from('<H', r_data, RESULT_DAY_OFFSET)[0] != day:
        fails.append('the result lost its day')
    if r_data[RESULT_WINNER_SCORE] != 67 or r_data[RESULT_YOUR_SCORE] != 72:
        fails.append('the result lost a score')
    if struct.unpack_from('<I', r_data, RESULT_PLACE)[0] != 1:
        fails.append('2nd place should be stored as 1 -- the client adds one')
    if struct.unpack_from('<I', r_data, RESULT_SPARE)[0]:
        fails.append('the unidentified u32 should be left alone')
    print('result:  %s wins with %d, viewer %d, 2nd place stored as %d'
          % (r_name, r_data[RESULT_WINNER_SCORE], r_data[RESULT_YOUR_SCORE],
             struct.unpack_from('<I', r_data, RESULT_PLACE)[0]))

    # 7d. par is a property of the COURSE, learnt from the cleanest card
    #
    # These are the six rounds the online server actually received, and the two
    # that disagree with the rest are the ones with triple-or-worse holes on
    # them.  The point of the test is that the MINIMUM bound is stable while
    # the per-card figure is not.
    dragon = [
        # strokes  EAGS BIRD PARS SBOG DBOG TBOG   -- all 18 holes, no aces
        (63, 1, 10, 5, 2, 0, 0),
        (77, 0, 6, 7, 1, 3, 1),
        (78, 0, 5, 8, 3, 1, 1),
        (74, 1, 9, 5, 0, 1, 2),
    ]
    cards = [dict(zip(('STROKES', 'EAGS', 'BIRD', 'PARS', 'SBOG', 'DBOG',
                       'TBOG'), row), ACES=0, HOLES=18) for row in dragon]
    bounds = [card_par(c) for c in cards]
    # The fourth card -- two triples -- bounds par at 77, which no course in
    # the game is, so PAR_RANGE throws it away rather than letting it vote.
    if bounds != [73, 73, 75, None]:
        fails.append('the four Emerald Dragon cards should bound par at '
                     '73/73/75/(rejected), got %r' % (bounds,))
    learnt = None
    for c in cards:
        learnt = learn_par(learnt, c)
    if learnt != 73:
        fails.append('the smallest bound is the par, expected 73 got %r'
                     % (learnt,))
    order = [to_par(c, learnt) for c in cards]
    if order != ['-10', '+4', '+5', '+1']:
        fails.append('one par for the field, got %r' % (order,))
    # The whole bug in one assertion: sorted by strokes, the to-par column has
    # to come out sorted too.  It did not when each card named its own par.
    by_strokes = sorted(cards, key=lambda c: c['STROKES'])
    figures = [c['STROKES'] - learnt for c in by_strokes]
    if figures != sorted(figures):
        fails.append('a better round must never read worse: %r' % (figures,))
    # A card with an ace in it is not evidence -- the model scores an ace -2
    # and one on a par 4 is -3, which would bound par BELOW the truth.
    if card_par(dict(cards[0], ACES=1, PARS=4)) is not None:
        fails.append('a card with an ace should not name a par')
    if learn_par(73, cards[2], known=71) != 71:
        fails.append('a par from a better source should win outright')
    if to_par({'STROKES': 72, 'HOLES': 18}, 72) != 'E':
        fails.append('a level round should read E')
    print('par:     four cards bound %r, learnt par %d, board reads %s'
          % (bounds, learnt, ' '.join(order)))

    purse = 2000000
    paid = [payout(purse, p) for p in range(1, 26)]
    if paid != sorted(paid, reverse=True):
        fails.append('a better finish must never pay less')
    if sum(paid) > purse:
        fails.append('the field cannot be paid more than the purse')
    if payout(purse, 0) or payout(purse, -1):
        fails.append('an unplaced player earns nothing')
    print('money:   winner %s of %s, 25th %s'
          % (money(paid[0]), money(purse), money(paid[24])))

    # 7e. the 2003 epoch, against the two rows that measured it
    base = to_day(LIST_EPOCH)
    if base != 37622:
        fails.append('2003-01-01 should be day 37622, got %d' % base)
    for sent, drew in ((72, datetime.date(2003, 3, 14)),
                       (1, datetime.date(2003, 1, 2))):
        if from_day(sent + base) != drew:
            fails.append('a row carrying %d should draw %s, got %s'
                         % (sent, drew, from_day(sent + base)))
    if to_list_day(base - 500):
        fails.append('a day before the epoch should clamp to 0, not go negative')
    # The measured ceiling: 8662 went out and 2025-06-05 came back, which is
    # 8191 days past the epoch.
    # A real day has to pass through untouched now -- clamping it here was
    # what kept the column at 2025 after the client's own limit was raised.
    if to_list_day(base + 8662) != 8662:
        fails.append('a day inside the patched range must not be clamped')
    if to_list_day(base + 0x9000) != LIST_DAY_MAX:
        fails.append('past the patched ceiling it should still clamp')
    if from_day(base + LIST_DAY_STOCK_MAX) != datetime.date(2025, 6, 5):
        fails.append('the stock ceiling should be 2025-06-05')
    inside = base + 1000
    if to_list_day(inside) + base != inside:
        fails.append('a day inside the range must round-trip exactly')
    print('epoch:   2003-01-01 is day %d; stock stops at %s, patched at %s'
          % (base, from_day(base + LIST_DAY_STOCK_MAX), LIST_LAST_DATE))

    # 8. the results list is narrower and must round-trip at its own width
    res = probe(30, day, RESULT_BYTES)
    body = encode_list(res, RESULT_BYTES)
    if decode_list(body, RESULT_BYTES) != res:
        fails.append('the 12-byte results list did not round-trip')
    if len(res) != RESULT_BYTES - 3:
        fails.append('the results probe should cover 9 offsets, not %d' % len(res))
    # and the widths really are different, or this whole distinction is moot.
    # Reading a results list as an info list must desynchronise, the same way
    # the client's parser did when it returned a count of zero.
    try:
        decode_list(encode_list(res, RESULT_BYTES), DATA_BYTES)
        fails.append('a 12-byte list should NOT read back as 16-byte entries')
    except ValueError:
        print('results list read at the info width: desynchronises, as it must')

    if fails:
        for f in fails:
            print('FAIL %s' % f)
        return 1
    print('\nok: hex reading stops where the client stops, lists round-trip\n'
          '    with literal names, and the empty list is "00"')
    return 0




# ---------------------------------------------------------------------------
# The round report (`5d0tr`, Tourn_ReportRoundResults)
#
# NOT ENCRYPTED.  `0x002DF6F0` calls 0x002C44A0/0x002C4570 around the buffer,
# which looked like a cipher from the disassembly alone; the captured `DATA` is
# plain little-endian struct data with the key copied into it.  Those calls are
# a serialiser, not a cipher.
#
# 128 bytes:
#
#     +0x00   23 x u32, the round -- the SAME field order as a `rank` result
#     +0x5C   the 16-byte TKEY the server issued from `esr2t`, echoed back
#     +0x6C   the event's own data[12:16]: the day, the course, and byte 15
#     +0x70   '$' + 12 hex characters, NUL padded -- unidentified
#
# So the anti-cheat is an echo, not a signature: a report is authentic if the
# key in it is the key this server handed that player when it let them start.
ROUND_FIELDS = ('RANK DONE QUIT TYPE SCORE CLUB STROKES PUTTS HOLES EAGS BIRD '
                'ACES GIR LPUT DRVS FRWY LDRV SHTC PARS SBOG DBOG TBOG '
                'COMP').split()
# TW05's report (Tourn_ReportRoundResults, 0x001D5E4C) is a 0x9C-byte struct:
# 0x70 bytes of stats copied from 0x003B37D0 (28 u32), the TKEY from
# 0x003B48E0 at 0x70, the event's day (u16, entry+0x30) at 0x80 and its
# course code (u32, entry+0x34) at 0x84, then a string at 0x89.  Hex-encoded
# by 0x001D3E70.  The MD5-style signing after it only runs when feature bit
# 4 of the `tfrst` reply is set, and ours is 0.
ROUND_FIELD_COUNT = 28
# Which word is which.  0x003B37D0 is also player 1's struct for the head-to-
# head `rank` report (Lobby_SendTwoPlayerResults, 0x001CB618; player 2 is at
# 0x003B3840, 0x70 on), which names every field it sends -- `lw a3, off($s1)`
# before each "KEY0" tag.  Words 3, 4, 8, 9 and 27 are not sent there and
# stay unnamed (X3, X4, X8, X9, X27).
#
# Two more are named by the first real report (2026-09-29, Greek Isles, 62):
# word 3 held 4, Greek Isles' index in the course table, and word 8 held 72,
# the course's par -- 72 - 2*2 eagles - 10 birdies + 4 bogeys = 62.  One
# round each, so treat them as likely rather than proven.  Words 4 (1), 9
# and 27 (0) are still unnamed.  After the struct, the tail string at 0x89
# is the console's MAC address ('$00041f82e366').
ROUND_WORDS = ('RANK', 'DONE', 'TYPE', 'CRSX', None, 'CLUB', 'QUIT', 'SCORE',
               'PAR', None, 'STROKES', 'PUTTS', 'HOLES', 'EAGS', 'BIRD', 'ACES',
               'GIR', 'LPUT', 'DRVS', 'FRWY', 'LDRV', 'SHTC', 'PARS', 'SBOG',
               'DBOG', 'TBOG', 'COMP', None)
ROUND_KEY_OFFSET = 0x70          # TW04: 92
ROUND_EVENT_OFFSET = 0x80        # u16 day (TW04: 108)
ROUND_COURSE_OFFSET = 0x84       # u32 course code
ROUND_TAIL_OFFSET = 0x89
ROUND_BYTES = 0x9C


def parse_round(text):
    """The `DATA` of a `5d0tr` -> (fields, key, day, course, tail).

    `text` is the hex exactly as it arrives.  Two identities make the field
    order checkable rather than assumed, and both held on the first real round:

        EAGS + BIRD + PARS + SBOG + DBOG + TBOG == HOLES
        72 - BIRD + SBOG + 2*DBOG + 3*TBOG      == STROKES
    """
    raw = bytes(bytearray.fromhex(text.strip()))
    if len(raw) < ROUND_TAIL_OFFSET:
        raise ValueError('a round is at least %d bytes, got %d'
                         % (ROUND_TAIL_OFFSET, len(raw)))
    values = struct.unpack_from('<%dI' % ROUND_FIELD_COUNT, raw, 0)
    fields = {name: values[i] for i, name in enumerate(ROUND_WORDS) if name}
    fields.update(('X%d' % i, values[i])
                  for i, name in enumerate(ROUND_WORDS) if not name)
    key = raw[ROUND_KEY_OFFSET:ROUND_KEY_OFFSET + 16]
    day = struct.unpack_from('<H', raw, ROUND_EVENT_OFFSET)[0]
    code = struct.unpack_from('<I', raw, ROUND_COURSE_OFFSET)[0]
    codes = [course_code(c) for c in COURSE_CODES]
    course = codes.index(code) if code in codes else None
    tail = raw[ROUND_TAIL_OFFSET:].split(b'\0')[0].decode('latin-1')
    return fields, key, day, course, tail


def round_is_consistent(f):
    """Do the scoring lines add up?  A cheap sanity check on a report.

    ACES is its OWN hole class, not a kind of eagle.  A card with a
    hole-in-one on it summed to 17 without it and was rejected as "that
    scorecard does not add up" -- a real round thrown away by the check meant
    to catch fake ones.
    """
    holes = sum(f.get(k, 0) for k in
                ('ACES', 'EAGS', 'BIRD', 'PARS', 'SBOG', 'DBOG', 'TBOG'))
    return holes == f.get('HOLES', 0) and f.get('HOLES', 0) > 0


# ---------------------------------------------------------------------------
# The RESULTS list (`qdb@w`)
#
# Same shape as the calendar, 12 data bytes instead of 16, and its day sits
# somewhere else entirely.  `0x002DF0D0(day, *count)` is "the results for day N":
#
#     0x002DF0EC  if day < *(u16 *)(first + 0x24):            return 0
#     0x002DF118  if *(u16 *)(last + 0x2C - 8) < day:         return 0
#     0x002DF140  for each entry: if *(u16 *)(e + 0x24) == day: keep the FIRST
#     0x002DF170  *count = how many matched
#
# `+0x24` is data offset **4**, and it counts the matches -- so a day holds many
# entries, one per player.  That is the daily leaderboard.
RESULT_DAY_OFFSET = 4

# The 12 bytes, named off EVENT RESULTS with each byte holding its own offset:
#
#     WINNER:       WINNER17        the entry NAME -- a player, not an event
#     WINNER SCORE: 6               u8 at 6
#     YOUR SCORE:   7               u8 at 7
#     YOUR FINISH:  185207049th     u32 at 8 -- 0x0B0A0908 is 185207048, and the
#                                   screen drew one more, so the place stored is
#                                   ZERO-BASED and the display adds 1
#
# Bytes 0..3 are the u32 `0x002DFFA0` reads; nothing on this dialog drew them,
# so they stay zero until something names them.
RESULT_WINNER_SCORE = 6          # u8
RESULT_YOUR_SCORE = 7            # u8
RESULT_PLACE = 8                 # u32, zero-based
RESULT_SPARE = 0                 # u32, read by 0x002DFFA0, unidentified


def probe_results(count, start, name='WINNER'):
    """One result a day, every byte but the day holding its own offset.

    The day goes to RESULT_DAY_OFFSET so the entry can be found at all; bytes
    0..3 then read as the u32 50462976, which is unmistakable on screen, and
    bytes 6..11 read as 6..11.  Whatever EVENT RESULTS draws, it names the byte
    behind it.
    """
    out = []
    for i in range(count):
        data = bytearray(range(RESULT_BYTES))
        struct.pack_into('<H', data, RESULT_DAY_OFFSET, start + i)
        out.append(('%s%02d' % (name, i), bytes(data)))
    return out


def make_result(winner, day, winner_score=0, your_score=0, place=1):
    """One day's result as a given player sees it.

    Note what an entry actually is: not "a player's round" but "the day, from
    one player's point of view".  The NAME is the winner, while the two scores
    and the finish are the viewer's own -- which is how the dialog draws all
    four lines from a single entry.

    `place` is passed 1-based, because that is how a person says it, and stored
    zero-based, because that is how the client reads it.
    """
    out = bytearray(RESULT_BYTES)
    struct.pack_into('<H', out, RESULT_DAY_OFFSET, day)
    out[RESULT_WINNER_SCORE] = min(255, max(0, winner_score))
    out[RESULT_YOUR_SCORE] = min(255, max(0, your_score))
    struct.pack_into('<I', out, RESULT_PLACE, max(0, place - 1))
    return (_ascii(winner)[:NAME_MAX], bytes(out))




def list_days(body, width=DATA_BYTES):
    """The days a list covers, for logging.

    The day is at a different offset in each list -- 12 in the calendar, 4 in
    the results -- which is exactly the kind of detail a log line should not
    have to know.
    """
    off = DAY_OFFSET if width == DATA_BYTES else RESULT_DAY_OFFSET
    return [struct.unpack_from('<H', data, off)[0]
            for _name, data in decode_list(body, width)]




# ---------------------------------------------------------------------------
# Presentation for the two type 2 lists, whose one free column is a string.

DEFAULT_PAR = 72                 # what a course is worth until one is learnt
PAR_RANGE = (67, 76)             # anything outside this is a misread, not a par


def card_par(fields):
    """The par this scorecard implies, or None when it cannot say.

    Section 45.8 read the hole-class counters as exact and took

        STROKES = par - BIRD - 2*EAGS + SBOG + 2*DBOG + 3*TBOG

    for an identity.  It is not one.  Six real rounds on two courses gave six
    different answers, and they were wrong in one direction only:

        Penguin Falls   63 strokes, no triples          -> par 72
        Emerald Dragon  63 strokes, no triples          -> par 73
        Emerald Dragon  77 strokes, one triple          -> par 73
        Emerald Dragon  78 strokes, one triple          -> par 75
        Emerald Dragon  74 strokes, two triples         -> par 77

    The more blow-up holes a card has, the higher the par it "implies" -- which
    is what you see when TBOG is really "triple bogey OR WORSE" and the strokes
    lost on those holes are not all counted.  A quadruple still adds one to
    TBOG and four to STROKES, and the formula only ever gives three back.

    So this is an UPPER BOUND on the course's par, never a lower one, and the
    smallest bound a course has ever produced is the best estimate of it.  See
    `learn_par`.

    An ace is the one thing that can push the bound the wrong way -- the model
    scores it -2, and an ace on a par 4 is -3 -- so a card with one in it is
    not used as evidence at all.
    """
    holes = sum(fields.get(k, 0) for k in
                ('ACES', 'EAGS', 'BIRD', 'PARS', 'SBOG', 'DBOG', 'TBOG'))
    if fields.get('ACES'):
        return None
    if holes != fields.get('HOLES', 0) or holes != 18:
        return None                      # part rounds cannot name a full par
    par = (fields.get('STROKES', 0)
           + fields.get('BIRD', 0) + 2 * fields.get('EAGS', 0)
           - fields.get('SBOG', 0) - 2 * fields.get('DBOG', 0)
           - 3 * fields.get('TBOG', 0))
    low, high = PAR_RANGE
    return par if low <= par <= high else None


def learn_par(seen, fields, known=None):
    """Fold one more scorecard into what is known about a course's par.

    `seen` is the smallest bound so far (or None), `known` a par from anywhere
    more trustworthy.  Smaller wins, because every bound errs high.
    """
    if known:
        return known
    bound = card_par(fields)
    if bound is None:
        return seen
    return bound if seen is None else min(seen, bound)


def to_par(fields, par=None):
    """A round as a golfer says it: "-5", "E", "+3".

    `par` is the COURSE's par and must be the same for everyone on that
    leaderboard.  Deriving it per card -- which is what this used to do -- gave
    three players on one course three different pars, and so a column of
    to-par figures that disagreed with the order they were in: 74 read -3 while
    78 read +3.  The strokes were right and the arithmetic around them was not.
    """
    strokes = fields.get('STROKES', 0)
    holes = fields.get('HOLES', 0) or 18
    par = par or DEFAULT_PAR
    if holes != 18:                      # nine holes is half a course
        par = int(round(par * holes / 18.0))
    diff = strokes - par
    return 'E' if diff == 0 else '%+d' % diff


# A purse is split down the field.  These are the PGA Tour's own top-ten
# percentages; past that it tapers rather than stopping dead, so a big field
# does not leave most of it on nothing.
# The client sorts leaderboard rows by their first word, DESCENDING, so a
# stroke-play board needs a key that grows as the round improves.  Subtracting
# from a base does that and keeps it a small positive number; the value is
# never drawn, only ordered by.
SORT_BASE = 200


PAYOUT = (0.18, 0.109, 0.068, 0.048, 0.04, 0.036, 0.0335, 0.031, 0.029, 0.027)


def payout(purse, place):
    """What `place` earns out of `purse`.  Place is 1-based; 0 earns nothing."""
    if place < 1:
        return 0
    if place <= len(PAYOUT):
        share = PAYOUT[place - 1]
    else:
        share = PAYOUT[-1] * (0.85 ** (place - len(PAYOUT)))
    return int(purse * share)


def money(amount):
    """"$1,234,567" -- the column is headed EARNINGS, so it should look like it."""
    return '$%s' % format(int(amount), ',')




# Lists 33, 34 and 35 count their days from a DIFFERENT ZERO.  `0x00272360`
# builds the day number of 1 January 2003 -- `0x00265570(&out, 1, 1, 0x7D3)` --
# and `0x002726FC` adds it to the row's own halfword before splitting it into a
# date.  So the row carries days since 2003, not the absolute day the calendar
# uses, and the two are 37622 apart.
#
# Measured, not assumed.  Two rows went out with 72 and 1 in that field and the
# screen drew 2003-03-14 and 2003-01-02 -- exactly 37622 past each.
LIST_EPOCH = datetime.date(2003, 1, 1)

# And the field is THIRTEEN BITS.  Sending 8662 drew 2025-06-05, which is 8191
# days past the epoch -- 0x1FFF exactly -- so the value saturates there.  The
# client's own splitter (0x00265640) is not the limit: it takes days since
# 1900-01-01 and re-bases anything past 36525 onto the year 2000, so it would
# have drawn 2026 quite happily.  Something upstream of it clips the row.
#
# TW04 shipped in 2003 and 8191 days from its epoch runs out in mid-2025, so
# this looks like a deliberate compact field rather than an accident.  Either
# way it is a hard ceiling: **these three lists cannot draw a date after
# 2025-06-05**, whatever the server sends.
# The master server patch raises the clamp at 0x002BCAEC from 0x1FFF to 0x7FFF
# and the value it writes from 0x1FFF to 0x7FFE, so a patched client takes days
# up to 32766 -- into 2092.  That patch is not optional (it ships with the lobby
# address and the DNAS bypass), so this follows it rather than the stock limit.
#
# An unpatched client is still safe: it clamps anything larger itself, exactly
# as it did before, and the column reads 2025-06-05.  Sending the real day is
# therefore right either way -- it is correct on a patched console and no worse
# than before on one without.
LIST_DAY_STOCK_MAX = 0x1FFF      # 2025-06-05, what the game ships with
LIST_DAY_MAX = 0x7FFE            # 2092-09-16, with the patch applied
LIST_LAST_DATE = LIST_EPOCH + datetime.timedelta(days=LIST_DAY_MAX)


def to_list_day(day):
    """An absolute day -> the day these three lists want.

    Clamped, because the alternative is not better: a value over the ceiling
    does not wrap to something wrong-but-plausible, it saturates, and the
    clamp at least makes that visible here instead of surprising in the log.
    """
    return max(0, min(LIST_DAY_MAX, day - to_day(LIST_EPOCH)))


if __name__ == '__main__':
    sys.exit(main())
