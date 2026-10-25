"""The sports rule behind the review app's "hide sports" filter."""
import json
import pytest
from refs.topics import is_sports


def row(projects, human=0, wd_sport=0, wd_other_job=0):
    return dict(projects=json.dumps(projects), human=human, wd_sport=wd_sport, wd_other_job=wd_other_job)


def test_people_with_another_main_occupation_stay_visible():
    assert not is_sports(row(["College basketball", "United States", "Women"], 1, 1, wd_other_job=1))  # Ashley Judd


@pytest.mark.parametrize("projects, human, wd_sport, sports", [
    (["Basketball", "Biography", "Chicago"], 1, 1, True),                      # Michael Jordan
    (["Football", "Spain"], 0, 0, True),                                       # a club or tournament: no Wikidata sport needed
    (["College football", "Politics", "Military history"], 1, 1, False),       # Eisenhower: another main topic
    (["Professional wrestling", "Metal", "Rock music"], 1, 0, False),          # Ozzy Osbourne
    (["National Football League", "United States", "Women"], 1, 0, False),     # an actor with a stray sports banner
    (["Football", "Cities", "Geography", "Mexico"], 0, 0, False),              # Mexico City
    (["Transport", "Biography"], 1, 0, False),                                 # 'sport' inside a word does not count
    (["Biography", "Film"], 1, 0, False),
])
def test_is_sports(projects, human, wd_sport, sports):
    assert is_sports(row(projects, human, wd_sport)) is sports
