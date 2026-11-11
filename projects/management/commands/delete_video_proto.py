"""
GRN-V0 cleanup (V0-8): list, then delete, every object under video-proto/ in
VIDEO_PROTO_BUCKET. Dry run by default; --apply deletes. Run it when the field test ends,
after turning VIDEO_PROTO_ENABLED off. Delete this command with the prototype.

    python manage.py delete_video_proto            # lists only
    python manage.py delete_video_proto --apply    # deletes, then lists what is left

It works with the flag off: the flag gates the pages, not the cleanup.
It never touches another bucket: video_proto._bucket_name() is empty when
VIDEO_PROTO_BUCKET is unset or names any SUPABASE_*_BUCKET, and the command then refuses.
It never deletes outside video-proto/: every path comes from listing that folder, and is
checked again before the delete. The ActivityLog lines stay (they hold no video).
"""
from django.core.management.base import BaseCommand, CommandError

from projects import video_proto

PAGE = 1000           # storage list() page size
REMOVE_BATCH = 100    # paths per remove() call
MAX_DEPTH = 3         # video-proto/<username>/<file>; deeper folders are walked but never made


def walk(api, folder, depth=0):
    """(path, size) for every object under `folder`, folders walked into. Supabase lists
    one folder level at a time, a folder being an entry with no id."""
    offset = 0
    while True:
        entries = api.list(folder, {'limit': PAGE, 'offset': offset,
                                    'sortBy': {'column': 'name', 'order': 'asc'}})
        for entry in entries:
            path = f"{folder}/{entry['name']}"
            if entry.get('id') is None:
                if depth < MAX_DEPTH:
                    yield from walk(api, path, depth + 1)
            else:
                yield path, (entry.get('metadata') or {}).get('size') or 0
        if len(entries) < PAGE:
            return
        offset += PAGE


class Command(BaseCommand):
    help = ('GRN-V0: list (dry run) or delete (--apply) every object under video-proto/ '
            'in VIDEO_PROTO_BUCKET.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Delete the listed objects. Without it nothing is deleted.')

    def handle(self, *args, **options):
        bucket = video_proto._bucket_name()
        if not bucket:
            raise CommandError('VIDEO_PROTO_BUCKET is unset, or names a bucket that holds '
                               'real files. Nothing listed or deleted.')
        api = video_proto.bucket_api()
        if api is None:
            raise CommandError('SUPABASE_URL or SUPABASE_KEY is unset. Nothing listed or deleted.')

        found = list(walk(api, video_proto.PREFIX))
        for path, size in found:
            self.stdout.write(f'  {path}  {size} bytes')
        total = sum(size for _, size in found)
        self.stdout.write(f'{len(found)} object(s), {total / 1e6:.1f} MB, under '
                          f'{video_proto.PREFIX}/ in bucket {bucket}.')

        if not options['apply']:
            self.stdout.write('Dry run: nothing deleted. Re-run with --apply to delete them.')
            return
        paths = [path for path, _ in found]
        # Belt and braces: listing video-proto/ can only yield paths under it, but a delete
        # is irreversible, so the prefix is asserted again on the exact list sent.
        if any(not path.startswith(video_proto.PREFIX + '/') for path in paths):
            raise CommandError('A listed path is outside video-proto/. Nothing deleted.')
        removed = 0
        for start in range(0, len(paths), REMOVE_BATCH):
            removed += len(api.remove(paths[start:start + REMOVE_BATCH]) or [])
        left = list(walk(api, video_proto.PREFIX))
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {removed} object(s). Left under {video_proto.PREFIX}/: {len(left)}.'))
