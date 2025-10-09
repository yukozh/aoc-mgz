"""MGZ tools."""

import asyncio
import argparse
import logging
import os
import struct
import sys
import json
from datetime import datetime
from collections import defaultdict

from construct.core import ConstructError
from tabulate import tabulate

import mgz
import mgz.const
import mgz.header
import mgz.util
from mgz.model import parse_match
from mgz import fast
from mgz.summary import Summary
from mgz.util import find_postgame, LOOKAHEAD
from mgz.model import parse_match, serialize


LOGGER = logging.getLogger(__name__)
CMD_INFO = 'info'
CMD_CHAT = 'chat'
CMD_JSON = 'json'
CMD_VALIDATE = 'validate'
CMD_DUMP = 'dump'
CMD_MERGE = 'merge'
CMD_HISTOGRAM = 'histogram'
CMD_PAD = 'pad'


def print_info(path):
    """Print basic info."""
    with open(path, 'rb') as handle:
        summary = Summary(handle)
        dataset = summary.get_dataset()
        print('-------------')
        print(tabulate([
            ['Path', path],
            ['Duration', mgz.util.convert_to_timestamp(summary.get_duration() / 1000)],
            ['Played', datetime.utcfromtimestamp(summary.get_played()) if summary.get_played() else None],
            ['Completed', summary.get_completed()],
            ['Restored', summary.get_restored()[0]],
            ['Postgame', bool(summary.get_postgame())],
            ['Version', '{} ({}, {}, {}, {})'.format(*summary.get_version())],
            ['Dataset', '{} {}'.format(dataset['name'], dataset['version'])],
            ['File Hash', summary.get_file_hash()],
            ['Match Hash', summary.get_hash().hexdigest() if summary.get_hash() else None],
            ['Encoding', summary.get_encoding()],
            ['Language', summary.get_language()],
            ['Device', summary.get_device()],
            ['Map', '{} ({}, {})'.format(summary.get_map()['name'], summary.get_map()['seed'], summary.get_map()['mod_id'])] # pylint: disable=unsubscriptable-object
        ], tablefmt='plain'))


def is_valid(path):
    """Validate a recorded game."""
    with open(path, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        try:
            mgz.header.parse_stream(handle)
            mgz.body.meta.parse_stream(handle)
            while handle.tell() < size:
                mgz.body.operation.parse_stream(handle)
            print('valid')
            return True
        except ConstructError:
            print('invalid')
            return False

def export_json(path):
    """Export to json."""
    with open(path, 'rb') as h:
        match = parse_match(h)
        print(json.dumps(serialize(match), indent=2))


def dump_rec(path):
    """Write parsed game to stdout."""
    with open(path, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        mgz.header.parse_stream(handle)
        while handle.tell() < size:
            operation = mgz.body.operation.parse_stream(handle)
            if operation.type == 'embedded':
                operation.data = '<snipped>'
            print(operation)


def print_chat(path):
    """Extract chat."""
    with open(path, 'rb') as handle:
        summary = Summary(handle)
        for c in summary.get_chat():
            print(c)


def merge_recs(part_one, part_two, output):
    """Merge two recorded games."""
    start_op_length = 28
    with open(part_one, 'rb') as a_handle, \
        open(part_two, 'rb') as b_handle, \
        open(output, 'wb') as merged:

        a_data = a_handle.read()
        b_data = b_handle.read()

        postgame_pos, _ = find_postgame(a_data, len(a_data))
        if postgame_pos:
            a_data_end = postgame_pos - LOOKAHEAD
        else:
            a_data_end = len(a_data)
        b_header_len, = struct.unpack('<I', b_data[:4])
        chapter = mgz.body.operation.build({
            'type': 'action',
            'op': 1,
            'length': 2,
            'action': {
                'type': 'chapter',
                'player_id': 0xff # our merge marker
            }
        })

        # part A with no postgame struct
        merged.write(a_data[:a_data_end])
        # chapter action
        merged.write(chapter)
        # offset to start of part B operations
        merged.write(struct.pack('<I', a_data_end + len(chapter) + b_header_len))
        # part B header (now a "saved chapter")
        merged.write(b_data[4:b_header_len])
        # part B operations with no start operation
        merged.write(b_data[b_header_len + start_op_length:])


def pad_rec(target_size, path, output):
    """Pad a recorded game."""
    with open(path, 'rb') as handle, open(output, 'wb') as padded:
        data = handle.read()
        data_length = len(data)
        padded.write(data)
        pad_length = target_size - data_length
        if pad_length < 9:
            raise ValueError('target size too small')
        pad_op = struct.pack('<IIB', 1, pad_length, 0xfe)
        pad_op += b'\x00' * (pad_length - 9)
        padded.write(pad_op)


def print_histogram(path):
    """Show operation and action histogram."""
    with open(path, 'rb') as handle:
        size = os.fstat(handle.fileno()).st_size
        mgz.header.parse_stream(handle)
        operations = defaultdict(int)
        actions = defaultdict(int)
        labels = {}
        fast.meta(handle)
        while handle.tell() < size:
            op_type, payload = fast.operation(handle)
            operations[op_type.name] += 1
            if op_type == fast.Operation.ACTION:
                action_id = payload[1]
                action_id = '{0:#0{1}x}'.format(payload[0].value, 4)
                labels[action_id] = payload[0].name
                actions[action_id] += 1
        print('Operations')
        print(tabulate([
            [operation, operations[operation]]
            for operation in sorted(operations, key=operations.get, reverse=True)
        ], headers=['Name', 'Count'], tablefmt='simple'))
        print()
        print('Actions')
        print(tabulate([
            [action, labels[action], actions[action]]
            for action in sorted(actions, key=actions.get, reverse=True)
        ], headers=['ID', 'Name', 'Count'], tablefmt='simple'))


async def run(args): # pylint: disable=too-many-branches
    """Entry point."""
    if args.cmd == CMD_INFO:
        for rec in args.rec_path:
            print_info(rec)
    elif args.cmd == CMD_CHAT:
        for rec in args.rec_path:
            print_chat(rec)
    elif args.cmd == CMD_JSON:
        for rec in args.rec_path:
            export_json(rec)
    elif args.cmd == CMD_VALIDATE:
        for rec in args.rec_path:
            if not is_valid(rec):
                sys.exit(1)
    elif args.cmd == CMD_DUMP:
        for rec in args.rec_path:
            dump_rec(rec)
    elif args.cmd == CMD_MERGE:
        merge_recs(args.part_one, args.part_two, args.output)
    elif args.cmd == CMD_HISTOGRAM:
        for rec in args.rec_path:
            print_histogram(rec)
    elif args.cmd == CMD_PAD:
        pad_rec(args.target_size, args.rec_path, args.output)
    await asyncio.sleep(0)


def get_args():
    """Get arguments."""
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='cmd')
    subparsers.required = True
    info = subparsers.add_parser(CMD_INFO)
    info.add_argument('rec_path', nargs='+')
    chat = subparsers.add_parser(CMD_CHAT)
    chat.add_argument('rec_path', nargs='+')
    json = subparsers.add_parser(CMD_JSON)
    json.add_argument('rec_path', nargs='+')
    validate = subparsers.add_parser(CMD_VALIDATE)
    validate.add_argument('rec_path', nargs='+')
    dump = subparsers.add_parser(CMD_DUMP)
    dump.add_argument('rec_path', nargs='+')
    merge = subparsers.add_parser(CMD_MERGE)
    merge.add_argument('part_one')
    merge.add_argument('part_two')
    merge.add_argument('output', default='merged.mgz')
    histogram = subparsers.add_parser(CMD_HISTOGRAM)
    histogram.add_argument('rec_path', nargs='+')
    pad = subparsers.add_parser(CMD_PAD)
    pad.add_argument('target_size', type=int)
    pad.add_argument('rec_path')
    pad.add_argument('output')
    return parser.parse_args()


def main():
    """Entry point."""
    logging.basicConfig(level='INFO')
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run(get_args()))


if __name__ == '__main__':
    main()
