# -*- coding: utf-8 -*-
"""
Base class for AiiDA tests
"""
from django.utils import unittest

# Add a new entry here if you add a file with tests under aiida.backends.djsite.db.subtests
# The key is the name to use in the 'verdi test' command (e.g., a key 'generic'
# can be run using 'verdi test db.generic')
# The value must be the module name containing the subclasses of unittest.TestCase

__copyright__ = u"Copyright (c), 2015, ECOLE POLYTECHNIQUE FEDERALE DE LAUSANNE (Theory and Simulation of Materials (THEOS) and National Centre for Computational Design and Discovery of Novel Materials (NCCR MARVEL)), Switzerland and ROBERT BOSCH LLC, USA. All rights reserved."
__license__ = "MIT license, see LICENSE.txt file"
__version__ = "0.4.1"
__contributors__ = "Andrea Cepellotti, Andrius Merkys, Eric Hontz," \
                   "Giovanni Pizzi, Nicolas Mounet, Martin Uhrin"

db_test_list = {
    'generic': 'aiida.backends.djsite.db.subtests.generic',
    'nodes': 'aiida.backends.djsite.db.subtests.nodes',
    'nwchem': 'aiida.backends.djsite.db.subtests.nwchem',
    'dataclasses': 'aiida.backends.djsite.db.subtests.dataclasses',
    'qepw': 'aiida.backends.djsite.db.subtests.quantumespressopw',
    'codtools': 'aiida.backends.djsite.db.subtests.codtools',
    'dbimporters': 'aiida.backends.djsite.db.subtests.dbimporters',
    'export_and_import': 'aiida.backends.djsite.db.subtests.export_and_import',
    'migrations': 'aiida.backends.djsite.db.subtests.migrations',
    'parsers': 'aiida.backends.djsite.db.subtests.parsers',
    'qepwinputparser': 'aiida.backends.djsite.db.subtests.pwinputparser',
    'qepwimmigrant': 'aiida.backends.djsite.db.subtests.quantumespressopwimmigrant',
    'tcodexporter': 'aiida.backends.djsite.db.subtests.tcodexporter',
    'workflows': 'aiida.backends.djsite.db.subtests.workflows',
}


class AiidaTestCase(unittest.TestCase):
    """
    Automatically takes care of the setUpClass and TearDownClass, when needed.
    """

    @classmethod
    def setUpClass(cls):

        from django.core.exceptions import ObjectDoesNotExist

        from aiida.backends.djsite.db.models import DbUser
        from aiida.orm.computer import Computer
        from aiida.backends.djsite.utils import get_configured_user_email

        # I create the user only once:
        # Otherwise, get_automatic_user() will fail when the
        # user is recreated because it caches the user!
        # In any case, store it in cls.user though
        # Other possibility: flush the user cache on delete
        try:
            cls.user = DbUser.objects.get(email=get_configured_user_email())
        except ObjectDoesNotExist:
            cls.user = DbUser.objects.create_user(get_configured_user_email(),
                                                  'fakepwd')
        cls.computer = Computer(name='localhost',
                                hostname='localhost',
                                transport_type='local',
                                scheduler_type='pbspro',
                                workdir='/tmp/aiida')
        cls.computer.store()

    @classmethod
    def tearDownClass(cls):
        from aiida.backends.djsite.db.models import DbComputer

        # I first delete the workflows
        from aiida.backends.djsite.db.models import DbWorkflow

        DbWorkflow.objects.all().delete()

        # Delete groups
        from aiida.backends.djsite.db.models import DbGroup

        DbGroup.objects.all().delete()

        # I first need to delete the links, because in principle I could
        # not delete input nodes, only outputs. For simplicity, since
        # I am deleting everything, I delete the links first
        from aiida.backends.djsite.db.models import DbLink

        DbLink.objects.all().delete()

        # Then I delete the nodes, otherwise I cannot
        # delete computers and users
        from aiida.backends.djsite.db.models import DbNode

        DbNode.objects.all().delete()

        ## I do not delete it, see discussion in setUpClass
        # try:
        #    DbUser.objects.get(email=get_configured_user_email()).delete()
        # except ObjectDoesNotExist:
        #    pass

        DbComputer.objects.all().delete()

        from aiida.backends.djsite.db.models import DbLog

        DbLog.objects.all().delete()
