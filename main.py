import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib

HEADERS = {'authorization': 'token '+ os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
QUERY_COUNT = {'user_getter': 0, 'graph_repos_stars': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}

RETRYABLE_STATUS_CODES = (502, 503, 504)
MAX_RETRIES = 8  # 1+2+4+8+16+30+30+30 = ~121s of total retry window per call


def daily_readme(birthday):
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    return 's' if unit != 1 else ''


def post_with_retry(query, variables):
    """POSTs to the GitHub GraphQL API, retrying on transient 502/503/504 errors
    with exponential backoff. Returns the final response object regardless of
    whether it ultimately succeeded — callers still check status_code themselves."""
    request = None
    for attempt in range(MAX_RETRIES):
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables': variables}, headers=HEADERS)
        if request.status_code == 200 or request.status_code not in RETRYABLE_STATUS_CODES:
            break
        wait = min(2 ** attempt, 30)
        print(f'Got {request.status_code} from GitHub API, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...')
        time.sleep(wait)
    return request


def simple_request(func_name, query, variables):
    request = post_with_retry(query, variables)
    try:
        payload = request.json()
    except ValueError:
        raise Exception(func_name, 'returned non-JSON response', request.status_code, request.text, QUERY_COUNT)

    if request.status_code != 200:
        raise Exception(func_name, ' has failed with a', request.status_code, payload, QUERY_COUNT)

    if isinstance(payload, dict) and 'errors' in payload:
        raise Exception(func_name, 'GraphQL returned errors:', payload['errors'], QUERY_COUNT)

    if isinstance(payload, dict) and 'data' not in payload:
        raise Exception(func_name, 'GraphQL response missing "data" key', payload, QUERY_COUNT)

    return request


def graph_commits(start_date, end_date):
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 25, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = post_with_retry(query, variables)

    if request.status_code == 200:
        payload = request.json()
        if isinstance(payload, dict) and 'errors' in payload:
            print(f'Warning: GraphQL errors for {owner}/{repo_name}: {payload["errors"]}. Skipping this repo for this run only — it will retry automatically next run.')
            return None
        if payload['data']['repository']['defaultBranchRef'] != None:
            return loc_counter_one_repo(owner, repo_name, data, cache_comment, payload['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
        else:
            return 0

    if request.status_code == 403:
        force_close_file(data, cache_comment)
        raise Exception('Too many requests in a short amount of time!\nYou\'ve hit the non-documented anti-abuse limit!')

    # Retries in post_with_retry were exhausted (persistent 502/503/504, or some
    # other non-200 status). Don't crash the whole script and don't zero out this
    # repo's data — just leave its cached line untouched. Because the commit-count
    # comparison in cache_builder will still see a mismatch, this exact repo will
    # automatically be retried on the next scheduled run.
    print(f'Warning: recursive_loc() for {owner}/{repo_name} failed with status {request.status_code} after {MAX_RETRIES} retries. Leaving cached value in place; will retry on next run.')
    return None


def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []

    if os.environ.get('SKIP_LOC') == '1':
        filename = 'cache/' + hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest() + '.txt'
        if os.path.exists(filename):
            try:
                with open(filename, 'r') as f:
                    data = f.readlines()
                cache_comment = data[:comment_size]
                data = data[comment_size:]
                loc_add = 0
                loc_del = 0
                for line in data:
                    parts = line.split()
                    if len(parts) >= 5:
                        loc_add += int(parts[3])
                        loc_del += int(parts[4])
                return [loc_add, loc_del, loc_add - loc_del, True]
            except Exception:
                print('SKIP_LOC=1 detected and cache file could not be read — returning zeros for LOC.')
                return [0, 0, 0, True]
        else:
            print('SKIP_LOC=1 detected — cache missing, returning zeros for LOC.')
            return [0, 0, 0, True]

    query_count('loc_query')

    if isinstance(owner_affiliation, (list, tuple)):
        aff_list = ', '.join(owner_affiliation)
    else:
        aff_list = str(owner_affiliation)
    affiliation_enum = '[' + aff_list + ']'

    query = f'''
    query ($login: String!, $cursor: String) {{
        user(login: $login) {{
            repositories(first: 60, after: $cursor, ownerAffiliations: {affiliation_enum}) {{
                edges {{
                    node {{
                        ... on Repository {{
                            nameWithOwner
                            defaultBranchRef {{
                                target {{
                                    ... on Commit {{
                                        history {{
                                            totalCount
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}
                pageInfo {{
                    endCursor
                    hasNextPage
                }}
            }}
        }}
    }}'''

    variables = {'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)

    payload = request.json()
    repos = payload['data']['user']['repositories']

    if repos['pageInfo']['hasNextPage']:
        edges += repos['edges']
        return loc_query(owner_affiliation, comment_size, force_cache, repos['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + repos['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' 
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    if loc is not None:
                        data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
                    # else: recursive_loc couldn't get this repo's data even after
                    # retries. Leave the existing cached line as-is — the commit
                    # count mismatch will persist, so this repo gets retried
                    # automatically on the next scheduled run. Nothing is lost.
            except TypeError:
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    data = []
    if comment_size > 0:
        for _ in range(comment_size):
            data.append('This line is a comment block. Write whatever you want here.\n')
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            line = hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n'
            f.write(line)


def add_archive():
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] 
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, loc_data):
    tree = etree.parse(filename)
    root = tree.getroot()
    justify_format(root, 'age_data', age_data)
    find_and_replace(root, 'age', age_data)
    justify_format(root, 'commit_data', commit_data)
    justify_format(root, 'star_data', star_data)
    justify_format(root, 'repo_data', repo_data)
    justify_format(root, 'contrib_data', contrib_data)
    justify_format(root, 'loc_data', loc_data[2])
    justify_format(root, 'loc_add', loc_data[0])
    justify_format(root, 'loc_del', loc_data[1])
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)

def find_and_replace(root, element_id, new_text):
    element = root.find(f".//*[@id='{element_id}']")
    if element is not None:
        element.text = new_text


def commit_counter(comment_size):
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError:
        return 0
    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for line in data:
        try:
            total_commits += int(line.split()[2])
        except Exception:
            continue
    return total_commits


def user_getter(username):
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def query_count(funct_id):
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    if difference > 1:
        print('{:>12}'.format('%.4f' % difference + ' s '))
    else:
        print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))

    if whitespace:
        try:
            formatted = format(funct_return, ',')
        except Exception:
            formatted = str(funct_return)
        return f"{formatted:<{whitespace}}"

    return funct_return


if __name__ == '__main__':
    print('Calculation times:')
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2005, 2, 12))
    formatter('age calculation', age_time)
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index])

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, total_loc[:-1])
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, total_loc[:-1])
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
