from bs4 import BeautifulSoup
from copy import copy
from datetime import datetime
from io import StringIO
from zipfile import ZipFile
import csv
import re
import shutil
from tempfile import NamedTemporaryFile, TemporaryDirectory
import urllib, urllib.request
from utils import request_and_parse, extract_attributes, \
   map_attributes, Fields, csv_sum


''' This file contains extra handling needed for some states
To make it work, the method must be called "handle_{state_abbreviation:lower_case}"
The parameters are:
- query result
- state mappings
'''

def atoi(val):
    if isinstance(val, int):
        return val
    return int(val.replace(",",''))

def handle_ar(res, mapping):
    tagged = {}
    for result in res[:-1]:
        partial = extract_attributes(result, mapping, 'AR')
        tagged.update(partial)

    soup = res[-1]
    try:
        tables = soup.find_all("table")
        table = tables[-1].find("tbody")
        for tr in table.find_all("tr"):
            cols = tr.find_all("td")
            if len(cols) < 2:
                 continue
            name = cols[0].get_text(strip=True)
            value = cols[1].get_text(strip=True)
            if name in mapping:
                tagged[mapping[name]] = atoi(value)

    except Exception as e:
        print(str(e))
        raise

    return tagged

def handle_ct(res, mapping):
    # res is a list of dict, one per day
    if not res or not res[0]:
        return {}
    res = res[0][0]
    mapped = map_attributes(res, mapping, 'CT')
    mapped[Fields.SPECIMENS.name] = mapped[Fields.TOTAL.name]
    return mapped

def handle_fl(res, mapping):
    '''Need to add the non-FL residents to the totals:
    they separate it for death and hosp"
    '''
    res = res[0]
    mapped = extract_attributes(res, mapping, 'FL')
    extra_hosp = 0
    extra_death = 0
    try:
        extra_hosp = res['features'][0]['attributes']['SUM_C_HospYes_NonRes']
        extra_death = res['features'][0]['attributes']['SUM_C_NonResDeaths']
    except Exception as ex:
        print("Failed Florida extra processing: ", str(e))
        raise

    mapped[Fields.HOSP.name] += extra_hosp
    mapped[Fields.DEATH.name] += extra_death

    return mapped

def handle_ky(res, mapping):
    tagged = {}
    for result in res[:-1]:
        partial = extract_attributes(result, mapping, 'MI')
        tagged.update(partial)

    # soup time
    soup = res[-1]
    h3 = soup.find("h3", string=re.compile("Coronavirus Monitoring"))
    if not h3:
        # quick fail
        return tagged

    datadiv = h3.find_next_sibling("div", "row")
    for item in datadiv.find_all("div", "info-card"):
        title = item.find("span", "title")
        value = item.find("span", "number")
        probable = item.find("span", "probable")
        if not value:
            continue

        hasalpha = lambda x: re.match("[a-zA-Z0-9]", x)
        pattern = "([a-zA-Z ]*): ([0-9,]*)"

        # class = title, number, probable
        title = title.get_text(strip=True)
        value = value.get_text(strip=True)
        probable = probable.get_text(strip=True) if probable else ""
        if probable:
            probable = re.findall(pattern, probable)

        if title.lower().find("tested") >= 0:
            for (k, v) in probable:
                if k.lower().find("pcr") >= 0:
                    tagged[Fields.TOTAL.name] = atoi(v)
                elif k.lower().find("serology") >= 0:
                    tagged[Fields.ANTIBODY_TOTAL.name] = atoi(v)
        elif title.lower().find("positive") >= 0:
            tagged[Fields.POSITIVE.name] = atoi(value)
            for (k, v) in probable:
                if k.lower().find("probable") >= 0:
                    tagged[Fields.PROBABLE.name] = atoi(v)
                elif k.lower().find("confirm") >= 0:
                    tagged[Fields.CONFIRMED.name] = atoi(v)
        elif title.lower().find("death") >= 0:
            tagged[Fields.DEATH.name] = atoi(value)
            for (k, v) in probable:
                if k.lower().find("probable") >= 0:
                    tagged[Fields.DEATH_PROBABLE.name] = atoi(v)
                elif k.lower().find("confirm") >= 0:
                    tagged[Fields.DEATH_CONFIRMED.name] = atoi(v)
        elif title.lower().find("recover") >= 0:
            tagged[Fields.RECOVERED.name] = atoi(value)

    updated = h3.find_next_sibling("p").get_text(strip=True)
    tagged[Fields.DATE.name] = updated

    return tagged

def handle_vt(res, mapping):
    state = 'VT'
    tagged = {}
    updated_mapping = copy(mapping)
    pui = 'hosp_pui'
    updated_mapping.update({pui: pui})
    for result in res:
        partial = extract_attributes(result, updated_mapping, state)
        tagged.update(partial)

    tagged[Fields.CURR_HOSP.name] += tagged[pui]
    tagged.pop(pui)
    return tagged

def handle_pa(res, mapping):
    '''Need to sum ECMO and Vent number for a total count
    '''
    state = 'PA'
    tagged = {}
    updated_mapping = copy(mapping)
    ecmo = 'ecmo'
    updated_mapping.update({ecmo: ecmo})
    for result in res[:-1]:
        partial = extract_attributes(result, updated_mapping, state)
        tagged.update(partial)

    tagged[Fields.CURR_VENT.name] += tagged[ecmo]
    tagged.pop(ecmo)

    # antibody stuff, soup time
    soup = res[-1]
    try:
        table = soup.find_all("table")[1]
        # expecting 2 rows, with 3 columns
        # need the value for "serology"
        rows = table.find_all('tr')
        titles = rows[0]
        data = rows[1].find_all('td')
        for i, title in enumerate(titles.find_all("td")):
            title = title.get_text(strip=True)
            if title.lower().find('serology') >= 0:
                value = atoi(data[i].get_text(strip=True))
                tagged[Fields.ANTIBODY_POS.name] = value

        if tagged.get(Fields.ANTIBODY_POS.name):
            tagged[Fields.POSITIVE.name] += tagged[Fields.ANTIBODY_POS.name]

    except Exception as e:
        pass

    return tagged

def handle_nm(res, mapping):
    data = res[0]['data']
    mapped = map_attributes(data, mapping, 'NM')
    return mapped

def handle_ne(res, mapping):
    tagged = {}
    for result in res[:-1]:
        partial = extract_attributes(result, mapping, 'NE')
        tagged.update(partial)
    stats = res[-1]
    if 'features' in stats and len(stats['features']) > 0:
        attributes = stats['features']
        for attr in attributes:
            # expecting {attributes: {lab_status: NAME, COUNT_EXPR0: VALUE}}
            name = attr['attributes']['lab_status']
            value = attr['attributes']['COUNT_EXPR0']
            if name in mapping:
                tagged[mapping[name]] = value

    return tagged

def handle_in(res, mapping):
    # ckan
    tagged = {}

    # There's pretty bad error handling now
    # I want to get errors as fast as possible -- to fix faster
    stats = res[0]['objects']['daily_statistics']
    tagged = map_attributes(stats, mapping, 'IN')

    hosp_data = res[1]['result']['records']
    for record in hosp_data:
        name = record['STATUS_TYPE']
        value = record['TOTAL']
        if name in mapping:
            tagged[mapping[name]] = value

    return tagged

def handle_la(res, mapping):
    stats = res[0]
    state_tests = 'STATE_TESTS'
    tagged = {}
    if 'features' in stats and len(stats['features']) > 0:
        attributes = stats['features']
        for attr in attributes:
            # expecting {attributes: {lab_status: NAME, COUNT_EXPR0: VALUE}}
            name = attr['attributes']['Measure']
            value = attr['attributes']['SUM_Value']
            if name in mapping:
                tagged[mapping[name]] = value

    if state_tests in tagged:
        tests = tagged.pop(state_tests)
        tagged[Fields.TOTAL.name] += tests

    # parse the probable death and vent and hospital data
    # This is going to be fragile
    hosp_title = "Reported COVID-19 Patients in Hospitals"
    recovered_title = "Presumed Recovered"
    curr_hosp = ""
    curr_vent = ""
    recovered = ""

    widgets = res[1].get('widgets', {})
    for widget in widgets:
        if widget.get('defaultSettings', {}) \
                    .get('topSection', {}).get('textInfo', {}).get('text') == hosp_title:
            # Take the hosp value from the main number, and vent number from the small text
            curr_hosp = atoi(widget['datasets'][0]['data'])
            vent_subtext = widget['defaultSettings']['bottomSection']['textInfo']['text']
            curr_vent = atoi(vent_subtext.split()[0])
        elif widget.get('defaultSettings', {}) \
                    .get('topSection', {}).get('textInfo', {}).get('text', '').find(recovered_title) >= 0:
            recovered = atoi(widget['datasets'][0]['data'])

    tagged[Fields.CURR_HOSP.name] = curr_hosp
    tagged[Fields.CURR_VENT.name] = curr_vent
    tagged[Fields.RECOVERED.name] = recovered
    return tagged

def handle_il(res, mapping):
    state = 'IL'
    state_name = 'Illinois'
    mapped = {}
    # main dashboard
    for county in res[0]['characteristics_by_county']['values']:
        if county['County'] == state_name:
            mapped = map_attributes(county, mapping, state)

    last_update = res[0]['LastUpdateDate']
    y = last_update['year']
    m = last_update['month']
    d = last_update['day']
    timestamp = datetime(y,m,d).timestamp()
    mapped[Fields.TIMESTAMP.name] = timestamp

    # hospital data
    hosp_data = res[1]['statewideValues']
    hosp_mapped = map_attributes(hosp_data, mapping, state)
    mapped.update(hosp_mapped)

    # fill in the date
    last_update = res[1]['LastUpdateDate']
    y = last_update['year']
    m = last_update['month']
    d = last_update['day']
    updated = datetime(y,m,d, 23, 59).strftime("%m/%d/%Y %H:%M:%S")
    mapped[Fields.DATE.name] = updated

    return mapped

def handle_gu(res, mapping):
    res = res[0]
    tagged = {}
    if 'features' in res and len(res['features']) > 0:
        attributes = res['features']
        for attr in attributes:
            # expecting {attributes: {Variable: NAME, Count: VALUE}}
            name = attr['attributes']['Variable']
            value = attr['attributes']['Count']
            if name in mapping:
                tagged[mapping[name]] = value

    # sum all tests
    return tagged

def handle_hi(res, mapping):
    res = res[0]

    # last row with values
    last_state_row = {}
    for row in res:
        if row['Region'] == 'State' and row.get('Cases_Tot'):
            last_state_row = row

    tagged = {}
    # expecting the order be old -> new data, so last line is the newest
    for k, v in last_state_row.items():
        if k in mapping:
            tagged[mapping[k]] = v

    return tagged

def handle_ri(res, mapping):
    dict_res = {r[0]: r[1] for r in res[0]}
    mapped = map_attributes(dict_res, mapping, 'RI')
    return mapped

def handle_ca(res, mapping):
    # ckan
    tagged = {}

    stats = res[0]['result']['records'][0]
    tagged = map_attributes(stats, mapping, 'CA')

    # add hosp and icu pui
    tagged[Fields.CURR_HOSP.name] = int(tagged[Fields.CURR_HOSP.name]) + int(stats.get('curr_hosp_pui', 0))
    tagged[Fields.CURR_ICU.name] = int(tagged[Fields.CURR_ICU.name]) + int(stats.get('curr_icu_pui', 0))
    return tagged

def handle_va(res, mapping):
    '''Getting multiple CVS files from the state and parsing each for
    the specific data it contains
    '''
    tagged = {}

    # Res:
    # 0 -- cases & death, probable & confirmed
    # 1 -- testing info
    # 2 -- hospital/icu/vent
    cases = res[0]
    testing = res[1]
    hospital = res[2]

    date_format = "%m/%d/%Y"

    # Cases
    # expecting 2 rows in the following format
    # Report Date,Case Status,Number of Cases,Number of Hospitalizations,Number of Deaths
    # 5/14/2020,Probable,1344,24,28
    # 5/14/2020,Confirmed,26469,3568,927

    PROB = 'Probable'
    CONF = 'Confirmed'

    for row in cases:
        if (row['Case Status'] == CONF):
            for k, v in row.items():
                if (k in mapping):
                    tagged[mapping[k]] = atoi(v)
        elif (row['Case Status'] == PROB):
            tagged[Fields.PROBABLE.name] = atoi(row['Number of Cases'])
            tagged[Fields.DEATH_PROBABLE.name] = atoi(row['Number of Deaths'])
    tagged[Fields.POSITIVE.name] = tagged[Fields.CONFIRMED.name] + tagged[Fields.PROBABLE.name]

    # sum everything
    testing_cols = [
        'Number of PCR Testing Encounters', 'Number of Positive PCR Tests',
        'Total Number of Testing Encounters', 'Total Number of Positive Tests']
    summed_testing = csv_sum(testing, testing_cols)
    tagged[Fields.SPECIMENS.name] = summed_testing[testing_cols[0]]
    tagged[Fields.SPECIMENS_POS.name] = summed_testing[testing_cols[1]]
    tagged[Fields.ANTIBODY_TOTAL.name] = summed_testing[testing_cols[2]] - summed_testing[testing_cols[0]]
    tagged[Fields.ANTIBODY_POS.name] = summed_testing[testing_cols[3]] - summed_testing[testing_cols[1]]

    # Hospitalizations
    hospital = sorted(hospital, key=lambda x: datetime.strptime(x['Date'], date_format), reverse = True)
    mapped_hosp = map_attributes(hospital[0], mapping, 'VA')
    tagged.update(mapped_hosp)

    return tagged

def handle_nj(res, mapping):
    '''Need to parse everything the same, and add past recoveries
    to the new query, because I do not know how to add a constant
    to the ArcGIS query
    '''
    mapped = {}
    for result in res:
        partial = extract_attributes(result, mapping, 'NJ')
        mapped.update(partial)

    mapped[Fields.RECOVERED.name] += 15642
    return mapped

def handle_ok(res, mapping):
    # need to sum all values
    res = res[0]

    # sum all fields
    # TODO: functools probably has something nice
    cols = ['Cases', 'Deaths', 'Recovered']
    summed = csv_sum(res, cols)
    mapped = map_attributes(summed, mapping, 'OK')
    mapped[Fields.DATE.name] = res[0].get('ReportDate')
    return mapped

def handle_ny(res, mapping):
    stats = res[0]
    mapped = map_attributes(stats[0], mapping, 'NY')

    return mapped

def handle_mi(res, mapping):
    tagged = {}
    for result in res[:-1]:
        partial = extract_attributes(result, mapping, 'MI')
        tagged.update(partial)

    # TODO: Can use the reverse mapping
    cases = 'Cases'
    deaths = 'Deaths'
    probable = 'Probable'
    confirmed = 'Confirmed'
    pcr = 'Diagnostic'
    antibody = 'Serology'
    negative = 'Negative'
    positive = 'Positive'

    df = res[3]
    filter_col = 'CASE_STATUS'
    summed = df.groupby(filter_col).sum()
    for m in [cases, deaths]:
        for t in [probable, confirmed]:
            tagged[mapping[m+t]] = summed[m][t]

    df = res[4]
    filter_col = 'TestType'
    summed = df.groupby(filter_col).sum()
    for m in [pcr, antibody]:
        tagged[mapping[m]] = summed['Count'][m]

    df = res[5]
    summed = df[[negative, positive]].sum()
    for x in [negative, positive]:
        tagged[mapping[x]] = summed[x]

    return tagged

def handle_nd(res, mapping):
    soup = res[-1]
    circles = soup.find_all("div", "circle")
    tagged = {}
    for c in circles:
        name = c.find('p').get_text(strip=True)
        value = atoi(c.find('h2').get_text(strip=True))
        if name in mapping:
            tagged[mapping[name]] = value

    # total tests
    tests_text = soup.find(string=re.compile("Tests Completed"))
    if tests_text:
        # take the 1st value, after stripping everything, including "-"
        tests_val = ""
        try:
            tests_text = tests_text.split()[0].split("-")[0]
            tests_val = atoi(tests_text)
        except Exception as e:
            print(str(e))
        if tests_val:
            tagged[Fields.SPECIMENS.name] = tests_val

    # Serology testing
    table = soup.find('table')
    rows = table.find_all('tr')
    titles = rows[0]
    data = rows[1].find_all('td')

    for i, title in enumerate(titles.find_all("td")):
        title = title.get_text(strip=True)
        if title in mapping:
            value = atoi(data[i].get_text(strip=True))
            tagged[mapping[title]] = value

    return tagged

def handle_ma(res, mapping):
    soup = res[0]
    link = soup.find('a', string=re.compile("COVID-19 Raw Data"))
    link_part = link['href']
    url = "https://www.mass.gov{}".format(link_part)

    tagged = {}

    # download zip
    req = urllib.request.Request(url, headers = {'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as response:
        with NamedTemporaryFile(delete=True) as tmpfile , TemporaryDirectory() as tmpdir:
            shutil.copyfileobj(response, tmpfile)
            shutil.unpack_archive(tmpfile.name, tmpdir, format="zip")

            # Now we can read the files
            files = ['DeathsReported.csv', 'Testing2.csv',
                     'Hospitalization from Hospitals.csv', 'Cases.csv']
            for filename in files:
                with open("{}/{}".format(tmpdir, filename), 'r') as csvfile:
                    reader = csv.DictReader(csvfile, dialect = 'unix')
                    rows = list(reader)
                    last_row = rows[-1]
                    partial = map_attributes(last_row, mapping, 'MA')
                    tagged.update(partial)

            hosp_key = ""
            for k, v in mapping.items():
                if v == Fields.HOSP.name:
                    hosp_key = k
            hospfile = csv.DictReader(open(tmpdir + "/RaceEthnicity.csv", 'r'))
            hosprows = list(hospfile)
            last_row = hosprows[-1]
            hosprows = [x for x in hosprows if x['Date'] == last_row['Date']]
            summed = csv_sum(hosprows, [hosp_key])
            tagged[Fields.HOSP.name] = summed[hosp_key]

    return tagged
