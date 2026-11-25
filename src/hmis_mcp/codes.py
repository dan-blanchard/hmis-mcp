"""HUD HMIS code lists used to label dimensions.

Codes follow the FY2024 and FY2026 HMIS CSV specifications, which share these
lists. FY2022 data is translated into them at load time. Unknown codes are
passed through as "Code <n>" so that a spec revision degrades to ugly labels,
not wrong ones.
"""

PROJECT_TYPE = {
    0: "Emergency Shelter - Entry/Exit",
    1: "Emergency Shelter - Night-by-Night",
    2: "Transitional Housing",
    3: "PH - Permanent Supportive Housing",
    4: "Street Outreach",
    6: "Services Only",
    7: "Other",
    8: "Safe Haven",
    9: "PH - Housing Only",
    10: "PH - Housing with Services",
    11: "Day Shelter",
    12: "Homelessness Prevention",
    13: "PH - Rapid Re-Housing",
    14: "Coordinated Entry",
}

# Project types where a client occupies a bed from MoveInDate, not EntryDate.
PH_PROJECT_TYPES = (3, 9, 10, 13)
NBN_PROJECT_TYPE = 1
# Project types that provide beds; bed utilization is undefined for the rest.
RESIDENTIAL_PROJECT_TYPES = (0, 1, 2, 3, 8, 9, 10, 13)

LIVING_SITUATION = {
    101: "Emergency shelter",
    116: "Place not meant for habitation",
    118: "Safe Haven",
    204: "Psychiatric hospital or facility",
    205: "Substance abuse treatment facility or detox",
    206: "Hospital or residential medical facility",
    207: "Jail, prison, or juvenile detention",
    215: "Foster care home or group home",
    225: "Long-term care facility or nursing home",
    302: "Transitional housing for homeless persons",
    312: "Staying with family, temporary",
    313: "Staying with friends, temporary",
    314: "Hotel or motel, no shelter voucher",
    327: "HOPWA transitional housing",
    329: "Residential project or halfway house",
    332: "Host Home (non-crisis)",
    335: "Staying in a family member's home",
    336: "Staying in a friend's home",
    410: "Rental by client, no ongoing subsidy",
    411: "Owned by client, no ongoing subsidy",
    421: "Owned by client, with ongoing subsidy",
    422: "Staying with family, permanent",
    423: "Staying with friends, permanent",
    426: "HOPWA permanent housing",
    435: "Rental by client, with ongoing subsidy",
    8: "Client doesn't know",
    9: "Client prefers not to answer",
    17: "Other",
    24: "Deceased",
    30: "No exit interview completed",
    37: "Worker unable to determine",
    99: "Data not collected",
}

# FY2022 living situation / destination codes -> FY2024 codes, per HUD Data
# Lab's conversion: each code gained a hundreds digit for its category, and the
# seven subsidized-rental codes collapsed into 435 (the subsidy type moved to a
# separate field, which this server doesn't use). Codes 8, 9, 17, 24, 30, 37
# and 99 are the same in both.
LEGACY_LIVING_SITUATION = {
    1: 101, 16: 116, 18: 118,
    4: 204, 5: 205, 6: 206, 7: 207, 15: 215, 25: 225,
    2: 302, 12: 312, 13: 313, 14: 314, 27: 327, 29: 329, 32: 332, 35: 335, 36: 336,
    10: 410, 11: 411, 21: 421, 22: 422, 23: 423, 26: 426,
    3: 435, 19: 435, 20: 435, 28: 435, 31: 435, 33: 435, 34: 435,
}
# FY2022 had one Emergency Shelter project type (1) plus a tracking method.
LEGACY_ES_PROJECT_TYPE = 1
LEGACY_TRACKING_NBN = 3

SITUATION_CATEGORIES = [
    "Homeless situation",
    "Institutional situation",
    "Temporary situation",
    "Permanent situation",
    "Deceased",
    "Other",
    "Unknown",
]

RELATIONSHIP_TO_HOH = {
    1: "Head of household",
    2: "Child of head of household",
    3: "Spouse or partner",
    4: "Other relative",
    5: "Unrelated household member",
}

# Race/ethnicity flag columns in Client.csv. The Hispanic column was renamed
# between spec versions, so it is matched by prefix at load time.
RACE_COLUMNS = {
    "AmIndAKNative": "American Indian, Alaska Native, or Indigenous",
    "Asian": "Asian or Asian American",
    "BlackAfAmerican": "Black, African American, or African",
    "MidEastNAfrican": "Middle Eastern or North African",
    "NativeHIPacific": "Native Hawaiian or Pacific Islander",
    "White": "White",
}
HISPANIC_LABEL = "Hispanic/Latina/o"

# Gender flag columns (element 3.06, retired in FY2026), under their FY2024
# names. FY2022's Female, Male and NoSingleGender load as the first three.
GENDER_COLUMNS = {
    "Woman": "Woman",
    "Man": "Man",
    "NonBinary": "Non-binary",
    "CulturallySpecific": "Culturally specific identity",
    "Transgender": "Transgender",
    "Questioning": "Questioning",
    "DifferentIdentity": "Different identity",
}

AGE_GROUPS = [
    (0, 4, "Under 5"),
    (5, 12, "5-12"),
    (13, 17, "13-17"),
    (18, 24, "18-24"),
    (25, 34, "25-34"),
    (35, 44, "35-44"),
    (45, 54, "45-54"),
    (55, 64, "55-64"),
    (65, 200, "65+"),
]

LOS_BUCKETS = [
    (0, 7, "0-7 days"),
    (8, 30, "8-30 days"),
    (31, 90, "31-90 days"),
    (91, 180, "91-180 days"),
    (181, 365, "181-365 days"),
    (366, 730, "366-730 days"),
    (731, 10**6, "731+ days"),
]

INCOME_BUCKETS = [
    (1, 500, "$1-500"),
    (501, 1000, "$501-1,000"),
    (1001, 1500, "$1,001-1,500"),
    (1501, 2000, "$1,501-2,000"),
    (2001, 10**9, "$2,001+"),
]

UNKNOWN = "Unknown"
YES_NO = ["Yes", "No", UNKNOWN]
