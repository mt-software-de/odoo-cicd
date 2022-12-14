*** Settings ***
Documentation       Repo setup a repository

Resource            ../addons_robot/robot_utils/keywords/odoo_community.robot
Resource            ../addons_robot/robot_utils_common/keywords/tools.robot
Resource            keywords.robot
Library             OperatingSystem
Library             ./cicd.py

Suite Setup         Setup Suite
Test Setup          Setup Test


*** Test Cases ***
Test Run Release 1
    [Documentation]    Normal release
    ${release_item_id}=    Prepare Release    deploy_git=${TRUE}

    ${branch_id}=  Make New Featurebranch
    ...    name=feature1
    ...    commit_name=New Feature1

    Odoo Write    cicd.git.branch    ${branch_id}    ${{ {'enduser_summary': "summary1"} }}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch_id}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch_id}
    Repeat Keyword    5 times    Release Heartbeat

    ${branches}=    Odoo Read Field    cicd.release.item    ${release_item_id}    branch_ids
    Should Be Equal As Strings    ${{str(len(${branches}))}}    1
    Release    ${release_item_id}

Test Run Release 2
    [Documentation]    Not deploying git directory
    ${release_id}=    Odoo Search    cicd.release    []    limit=1
    Odoo Write    cicd.release    ${release_id[0]}    ${{ {'deploy_git': False} }}
    ${branch_id}=  Make New Featurebranch
    ...    name=feature2
    ...    commit_name=New Feature2
    ${release_item_id}=    Odoo Search    cicd.release.item    []    limit=1    order=id desc
    ${state}=    Odoo Read Field    cicd.release.item    ${release_item_id[0]}    state
    Should Be Equal As Strings    ${state}    collecting

    Odoo Write    cicd.git.branch    ${branch_id}    ${{ {'enduser_summary': "summary feature2"} }}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch_id}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch_id}
    Repeat Keyword    5 times    Release Heartbeat

    Release    ${release_item_id}

Test Block Deployment
    ${release_item_id}=    Odoo Search    cicd.release.item    []    limit=1    order=id desc
    ${state}=    Odoo Read Field    cicd.release.item    ${release_item_id[0]}    state
    Should Be Equal As Strings    ${state}    collecting

    Make New Featurebranch
    ...    name=blocked1
    ...    commit_name=Blocked
    Log To Console    The branch should be now already in the deployment; and now
    ...    it is decided to block it in last second; so check if
    ...    deployment is stopped
    ${branch_id}=    Odoo Search    cicd.git.branch    [('name', '=', 'blocked1')]
    Odoo Write    cicd.git.branch    ${branch_id}    ${{ {'block_release': True } }}
    Release    release_item_id=${release_item_id}  expected_state=done_nothing_todo

Test Merge Conflict
    [Documentation]    Checks if albeit merge conflicts a release happens

    ${release_item_id}=    Odoo Search    cicd.release.item    []    limit=1    order=id desc
    ${state}=    Odoo Read Field    cicd.release.item    ${release_item_id[0]}    state
    Should Be Equal As Strings    ${state}    collecting

    # Change the same file like in feature1 so that a conflict happens
    ${branch3}=  Make New Featurebranch
    ...    name=feature3
    ...    commit_name=New Feature2 should make a conflict
    ...    filetotouch=samefile
    ...    filecontent=something_else

    ${branch4}=  Make New Featurebranch
    ...    name=feature4
    ...    commit_name=New Feature3 should make a conflict
    ...    filetotouch=samefile
    ...    filecontent=something_else4

    Odoo Write    cicd.git.branch    ${branch3}    ${{ {'enduser_summary': "summary2"} }}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch3}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch3}
    Sleep  3s
    Odoo Write    cicd.git.branch    ${branch4}    ${{ {'enduser_summary': "summary3"} }}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch4}
    Odoo Execute    cicd.git.branch    method=set_approved    ids=${branch4}
    Repeat Keyword    5 times    Release Heartbeat

    ${state}=  Odoo Read Field    cicd.release.item    ${release_item_id}    state
    Should Be Equal As Strings    ${state}    collecting_merge_conflict


    FAIL  check that feature3 is really online