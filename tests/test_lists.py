"""List system: CRUD, shopping alias, ownership, isolation."""
ALICE = {'Cf-Access-Authenticated-User-Email': 'alice@example.com'}
BOB = {'Cf-Access-Authenticated-User-Email': 'bob@example.com'}


def test_list_crud_and_items(server):
    serve, httpd, client, tmp = server
    # The shopping list always exists.
    status, body = client.get('/todo/api/lists', headers=ALICE)
    assert status == 200
    assert any(l['id'] == 'shopping' for l in body['lists'])
    # Create a custom list
    status, body = client.post('/todo/api/lists',
                               body={'name': ' groceries '}, headers=ALICE)
    assert status == 201
    assert body['id'] == 'groceries'      # name is stripped; id = stripped name
    lid = body['id']
    # Add items
    status, body = client.post(f'/todo/api/lists/{lid}/items',
                               body={'title': '鸡蛋'}, headers=ALICE)
    assert status == 201
    item_id = body['id']
    status, body = client.get(f'/todo/api/lists/{lid}/items', headers=ALICE)
    assert [i['title'] for i in body['items']] == ['鸡蛋']
    # Toggle done
    status, _ = client.put(f'/todo/api/lists/{lid}/items/{item_id}',
                           body={'status': 'done'}, headers=ALICE)
    assert status == 200
    status, body = client.get(f'/todo/api/lists/{lid}/items', headers=ALICE)
    assert body['items'][0]['status'] == 'done'
    # Edit title (title = file body, uuid filename unchanged)
    status, _ = client.put(f'/todo/api/lists/{lid}/items/{item_id}',
                           body={'title': '鸡蛋（一打）'}, headers=ALICE)
    assert status == 200
    status, body = client.get(f'/todo/api/lists/{lid}/items', headers=ALICE)
    assert body['items'][0]['title'] == '鸡蛋（一打）'
    assert body['items'][0]['id'] == item_id      # id stable
    # Delete item
    status, _ = client.delete(f'/todo/api/lists/{lid}/items/{item_id}', headers=ALICE)
    assert status == 200
    status, body = client.get(f'/todo/api/lists/{lid}/items', headers=ALICE)
    assert body['items'] == []
    # Delete list
    status, _ = client.delete(f'/todo/api/lists/{lid}', headers=ALICE)
    assert status == 200
    status, body = client.get('/todo/api/lists', headers=ALICE)
    assert all(l['id'] != lid for l in body['lists'])


def test_shopping_alias(server):
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/shopping',
                               body={'title': '牛奶'}, headers=ALICE)
    assert status == 201
    # The alias endpoint sees the same data as lists/shopping.
    status, body = client.get('/todo/api/shopping', headers=ALICE)
    assert [i['title'] for i in body['items']] == ['牛奶']
    status, body = client.get('/todo/api/lists/shopping/items', headers=ALICE)
    assert [i['title'] for i in body['items']] == ['牛奶']


def test_shopping_dedupe_restores_done_item(server):
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/shopping',
                               body={'title': '牛奶', 'dedupe': True}, headers=ALICE)
    assert status == 201
    item_id = body['id']
    client.put(f'/todo/api/shopping/{item_id}', body={'status': 'done'}, headers=ALICE)
    # Adding the same title with dedupe revives the done item instead of duplicating.
    status, body = client.post('/todo/api/shopping',
                               body={'title': '牛奶', 'dedupe': True}, headers=ALICE)
    assert status == 200
    assert body.get('duplicate') is True
    status, body = client.get('/todo/api/shopping', headers=ALICE)
    assert len(body['items']) == 1
    assert body['items'][0]['status'] == 'todo'


def test_list_ownership_toggle(server):
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/lists',
                               body={'name': '私list'}, headers=ALICE)
    lid = body['id']
    assert body['owner'] == 'alice'
    # Bob cannot see alice's private list.
    status, body = client.get('/todo/api/lists', headers=BOB)
    assert all(l['id'] != lid for l in body['lists'])
    # Bob cannot reassign alice's PRIVATE list either.
    status, _ = client.put(f'/todo/api/lists/{lid}',
                           body={'owner': 'shared'}, headers=BOB)
    assert status == 403
    # Share it (owner action).
    status, body = client.put(f'/todo/api/lists/{lid}',
                              body={'owner': 'shared'}, headers=ALICE)
    assert status == 200
    status, body = client.get('/todo/api/lists', headers=BOB)
    assert any(l['id'] == lid for l in body['lists'])
    # Bob can now add items.
    status, _ = client.post(f'/todo/api/lists/{lid}/items',
                            body={'title': 'bob的物品'}, headers=BOB)
    assert status == 201
    # Existing behavior (preserved): a shared list can be "claimed" and
    # made private by any user who acts on it.
    status, body = client.put(f'/todo/api/lists/{lid}',
                              body={'owner': 'self'}, headers=BOB)
    assert status == 200
    assert body['owner'] == 'bob'


def test_list_item_owner_follows_list(server):
    serve, httpd, client, tmp = server
    status, body = client.post('/todo/api/lists',
                               body={'name': '搬家清单'}, headers=ALICE)
    lid = body['id']
    status, body = client.post(f'/todo/api/lists/{lid}/items',
                               body={'title': '纸箱'}, headers=ALICE)
    assert status == 201
    # Items inherit the list owner.
    status, body = client.get(f'/todo/api/lists/{lid}/items', headers=ALICE)
    assert body['items'][0]['owner'] == 'alice'
