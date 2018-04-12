import random
from datetime import datetime
from typing import List

import dateutil.parser as parser

MIN_DATE = "2000-01-01T00:00:00"
MAX_DATE = "2014-08-29T23:59:59"

STATEMENTS = {
    'BF03': 'SELECT VALUE u '
            'FROM `GleambookUsers` u '
            'WHERE u.user_since >= "{}" AND u.user_since < "{}";',
    'BF04': 'SELECT VALUE u '
            'FROM `GleambookUsers` u '
            'WHERE u.user_since >= "{}" AND u.user_since < "{}" '
            'AND (SOME e IN u.employment SATISFIES e.end_date IS UNKNOWN);',
    'BF08': 'SELECT cm.user.screen_name AS username, AVG(LENGTH(cm.message_text)) AS avg '
            'FROM `ChirpMessages` cm '
            'WHERE cm.send_time >= "{}" AND cm.send_time < "{}" '
            'GROUP BY cm.user.screen_name '
            'ORDER BY avg '
            'LIMIT 10;',
    'BF14': 'SELECT META(u).id AS id, COUNT(*) AS count '
            'FROM `GleambookUsers` u, `GleambookMessages` m '
            'WHERE TO_STRING(META(u).id) = m.author_id '
            'AND u.user_since >= "{}" AND u.user_since < "{}" '
            'AND m.send_time >= "{}" AND m.send_time < "{}" '
            'GROUP BY META(u).id;',
    'BF15': 'SELECT META(u).id AS id, COUNT(*) AS count '
            'FROM `GleambookUsers` u, `GleambookMessages` m '
            'WHERE TO_STRING(META(u).id) = m.author_id '
            'AND u.user_since >= "{}" AND u.user_since < "{}" '
            'AND m.send_time >= "{}" AND m.send_time < "{}" '
            'GROUP BY META(u).id '
            'ORDER BY count '
            'LIMIT 10;',
}


def iso2seconds(dt: str) -> int:
    return int(parser.parse(dt).strftime('%s'))


def seconds2iso(s: int) -> str:
    return datetime.fromtimestamp(s).strftime('%Y-%m-%dT%H:%M:%S')


def min_timestamp() -> int:
    return iso2seconds(MIN_DATE)


def max_timestamp() -> int:
    return iso2seconds(MAX_DATE)


def interval() -> int:
    return max_timestamp() - min_timestamp()


def items_per_second(dataset: str) -> float:
    return {
        'ChirpMessages': 2e8 / interval(),
        'GleambookMessages': 1e8 / interval(),
        'GleambookUsers': 2e7 / interval(),
    }[dataset]


def new_offset(seconds: int) -> int:
    return random.randint(min_timestamp(), max_timestamp() - seconds)


def new_dates(dataset: str, num_matches: float) -> List[str]:
    seconds = int(num_matches / items_per_second(dataset))
    offset = new_offset(seconds)
    return [seconds2iso(offset), seconds2iso(offset + seconds)]


def bf03params(num_matches: float) -> List[str]:
    return new_dates('GleambookUsers', num_matches)


def bf04params(num_matches: float) -> List[str]:
    return bf03params(num_matches)


def bf08params(num_matches: float) -> List[str]:
    return new_dates('ChirpMessages', num_matches)


def bf14params(num_matches: float) -> List[str]:
    return new_dates('GleambookUsers', num_matches) + \
        new_dates('GleambookMessages', num_matches)


def bf15params(num_matches: float) -> List[str]:
    return bf14params(num_matches)


def new_params(name: str, num_matches: float) -> List[str]:
    return {
        'BF03': bf03params(num_matches),
        'BF04': bf04params(num_matches),
        'BF08': bf08params(num_matches),
        'BF14': bf14params(num_matches),
        'BF15': bf15params(num_matches),
    }[name]


def new_statement(query: dict) -> str:
    params = new_params(query['name'], query['matches'])
    return STATEMENTS[query['name']].format(*params)
