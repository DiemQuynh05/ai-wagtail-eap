from wagtail import hooks
from wagtail.admin.menu import MenuItem

@hooks.register('register_admin_menu_item')
def register_dashboard_menu_item():
    return MenuItem(
        'Dashboard ERP', 
        '/api/dashboard/', 
        classname='icon icon-fa-bar-chart', 
        order=100
    )
