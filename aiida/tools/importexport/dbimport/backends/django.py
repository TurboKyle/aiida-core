# -*- coding: utf-8 -*-
###########################################################################
# Copyright (c), The AiiDA team. All rights reserved.                     #
# This file is part of the AiiDA code.                                    #
#                                                                         #
# The code is hosted on GitHub at https://github.com/aiidateam/aiida-core #
# For further information on the license, see the LICENSE.txt file        #
# For further information please visit http://www.aiida.net               #
###########################################################################
# pylint: disable=protected-access,fixme,too-many-arguments,too-many-locals,too-many-statements,too-many-branches,too-many-nested-blocks
""" Django-specific import of AiiDA entities """
from itertools import chain
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
import warnings

from aiida.common.links import LinkType, validate_link_label
from aiida.common.progress_reporter import get_progress_reporter
from aiida.common.utils import get_object_from_string, validate_uuid
from aiida.common.warnings import AiidaDeprecationWarning
from aiida.manage.configuration import get_config_option
from aiida.orm import Group

from aiida.tools.importexport.common import exceptions
from aiida.tools.importexport.common.config import DUPL_SUFFIX
from aiida.tools.importexport.common.config import (
    NODE_ENTITY_NAME, GROUP_ENTITY_NAME, COMPUTER_ENTITY_NAME, USER_ENTITY_NAME, LOG_ENTITY_NAME, COMMENT_ENTITY_NAME
)
from aiida.tools.importexport.common.config import entity_names_to_signatures
from aiida.tools.importexport.dbimport.utils import (
    deserialize_field, merge_comment, merge_extras, start_summary, result_summary, IMPORT_LOGGER
)

from aiida.tools.importexport.archive.common import detect_archive_type
from aiida.tools.importexport.archive.readers import ArchiveReaderAbstract, get_reader

from aiida.tools.importexport.dbimport.backends.common import (
    _copy_node_repositories, _make_import_group, _sanitize_extras, _strip_checkpoints, MAX_COMPUTERS, MAX_GROUPS
)


def import_data_dj(
    in_path: str,
    group: Optional[Group] = None,
    ignore_unknown_nodes: bool = False,
    extras_mode_existing: str = 'kcl',
    extras_mode_new: str = 'import',
    comment_mode: str = 'newest',
    silent: Optional[bool] = None,
    **kwargs: Any
):  # pylint: disable=unused-argument
    """Import exported AiiDA archive to the AiiDA database and repository.

    Specific for the Django backend.
    If ``in_path`` is a folder, calls extract_tree; otherwise, tries to detect the compression format
    (zip, tar.gz, tar.bz2, ...) and calls the correct function.

    :param in_path: the path to a file or folder that can be imported in AiiDA.
    :type in_path: str

    :param group: Group wherein all imported Nodes will be placed.
    :type group: :py:class:`~aiida.orm.groups.Group`

    :param extras_mode_existing: 3 letter code that will identify what to do with the extras import.
        The first letter acts on extras that are present in the original node and not present in the imported node.
        Can be either:
        'k' (keep it) or
        'n' (do not keep it).
        The second letter acts on the imported extras that are not present in the original node.
        Can be either:
        'c' (create it) or
        'n' (do not create it).
        The third letter defines what to do in case of a name collision.
        Can be either:
        'l' (leave the old value),
        'u' (update with a new value),
        'd' (delete the extra), or
        'a' (ask what to do if the content is different).
    :type extras_mode_existing: str

    :param extras_mode_new: 'import' to import extras of new nodes or 'none' to ignore them.
    :type extras_mode_new: str

    :param comment_mode: Comment import nodes (when same UUIDs are found).
        Can be either:
        'newest' (will keep the Comment with the most recent modification time (mtime)) or
        'overwrite' (will overwrite existing Comments with the ones from the import file).
    :type comment_mode: str

    :return: New and existing Nodes and Links.
    :rtype: dict

    :raises `~aiida.tools.importexport.common.exceptions.ImportValidationError`: if parameters or the contents of
        `metadata.json` or `data.json` can not be validated.
    :raises `~aiida.tools.importexport.common.exceptions.CorruptArchive`: if the provided archive at ``in_path`` is
        corrupted.
    :raises `~aiida.tools.importexport.common.exceptions.IncompatibleArchiveVersionError`: if the provided archive's
        export version is not equal to the export version of AiiDA at the moment of import.
    :raises `~aiida.tools.importexport.common.exceptions.ArchiveImportError`: if there are any internal errors when
        importing.
    :raises `~aiida.tools.importexport.common.exceptions.ImportUniquenessError`: if a new unique entity can not be
        created.
    """
    # Initial check(s)
    if silent is not None:
        warnings.warn(
            'silent keyword is deprecated and will be removed in AiiDA v2.0.0, set the logger level explicitly instead',
            AiidaDeprecationWarning
        )  # pylint: disable=no-member

    if extras_mode_new not in ['import', 'none']:
        raise exceptions.ImportValidationError(
            f"Unknown extras_mode_new value: {extras_mode_new}, should be either 'import' or 'none'"
        )
    reader_cls = get_reader(detect_archive_type(in_path))

    if group:
        if not isinstance(group, Group):
            raise exceptions.ImportValidationError('group must be a Group entity')
        elif not group.is_stored:
            group.store()

    # The returned dictionary with new and existing nodes and links
    # entity_name -> new or existing -> list pk
    ret_dict: Dict[str, dict] = {}

    with reader_cls(in_path) as reader:

        IMPORT_LOGGER.debug('Checking archive version compatibility')

        reader.check_version()

        start_summary(in_path, comment_mode, extras_mode_new, extras_mode_existing)

        ##########################################################################
        # CREATE UUID REVERSE TABLES AND CHECK IF I HAVE ALL NODES FOR THE LINKS #
        ##########################################################################
        IMPORT_LOGGER.debug('CHECKING IF NODES FROM LINKS ARE IN DB OR ARCHIVE...')

        linked_nodes = set(chain.from_iterable((l['input'], l['output']) for l in reader.iter_link_data()))
        group_nodes = set(chain.from_iterable((uuids for _, uuids in reader.iter_group_uuids())))

        # Check that UUIDs are valid
        linked_nodes = set(x for x in linked_nodes if validate_uuid(x))
        group_nodes = set(x for x in group_nodes if validate_uuid(x))

        import_nodes_uuid = set(v for v in reader.iter_node_uuids())

        # the combined set of linked_nodes and group_nodes was obtained from looking at all the links
        # the set of import_nodes_uuid was received from the stuff actually referred to in export_data
        unknown_nodes = linked_nodes.union(group_nodes) - import_nodes_uuid

        if unknown_nodes and not ignore_unknown_nodes:
            raise exceptions.DanglingLinkError(
                'The import file refers to {} nodes with unknown UUID, therefore it cannot be imported. Either first '
                'import the unknown nodes, or export also the parents when exporting. The unknown UUIDs are:\n'
                ''.format(len(unknown_nodes)) + '\n'.join('* {}'.format(uuid) for uuid in unknown_nodes)
            )

        ###################################
        # DOUBLE-CHECK MODEL DEPENDENCIES #
        ###################################
        # The entity import order. It is defined by the database model relationships.
        entity_order = (
            USER_ENTITY_NAME, COMPUTER_ENTITY_NAME, NODE_ENTITY_NAME, GROUP_ENTITY_NAME, LOG_ENTITY_NAME,
            COMMENT_ENTITY_NAME
        )

        for entity_name in reader.entity_names:
            if entity_name not in entity_order:
                raise exceptions.ImportValidationError(f"You are trying to import an unknown model '{entity_name}'!")

        for idx, entity_name in enumerate(entity_order):
            dependencies = []
            for field in reader.metadata.all_fields_info[entity_name].values():
                try:
                    dependencies.append(field['requires'])
                except KeyError:
                    # (No ForeignKey)
                    pass
            for dependency in dependencies:
                if dependency not in entity_order[:idx]:
                    raise exceptions.ArchiveImportError(
                        f'Entity {entity_name} requires {dependency} but would be loaded first; stopping...'
                    )

        IMPORT_LOGGER.debug('CREATING PK-2-UUID/EMAIL MAPPING...')
        # entity_name -> pk -> unique id
        import_unique_ids_mappings: Dict[str, Dict[int, str]] = {}
        for entity_name, identifier in reader.metadata.unique_identifiers.items():
            import_unique_ids_mappings[entity_name] = {
                int(k): f[identifier] for k, f in reader.iter_entity_fields(entity_name, fields=(identifier,))
            }

        # count total number of entities to import
        number_of_entities: int = sum(reader.entity_count(entity_name) for entity_name in entity_order)
        IMPORT_LOGGER.debug('Importing %s entities', number_of_entities)

        ###########################################
        # IMPORT ALL DATA IN A SINGLE TRANSACTION #
        ###########################################
        from django.db import transaction  # pylint: disable=import-error,no-name-in-module

        # batch size for bulk create operations
        batch_size: int = get_config_option('db.batch_size')

        with transaction.atomic():

            # entity_name -> str(pk) -> fields
            new_entries: Dict[str, Dict[str, dict]] = {}
            existing_entries: Dict[str, Dict[str, dict]] = {}
            # entity_name -> identifier -> pk
            foreign_ids_reverse_mappings: Dict[str, Dict[str, int]] = {}

            IMPORT_LOGGER.debug('ASSESSING IMPORT DATA...')
            for entity_name in entity_order:
                _select_entity_data(
                    entity_name=entity_name,
                    reader=reader,
                    new_entries=new_entries,
                    existing_entries=existing_entries,
                    foreign_ids_reverse_mappings=foreign_ids_reverse_mappings,
                    extras_mode_new=extras_mode_new,
                )

            IMPORT_LOGGER.debug('STORING ENTITIES...')
            for entity_name in entity_order:
                _store_entity_data(
                    reader=reader,
                    entity_name=entity_name,
                    comment_mode=comment_mode,
                    extras_mode_existing=extras_mode_existing,
                    new_entries=new_entries,
                    existing_entries=existing_entries,
                    foreign_ids_reverse_mappings=foreign_ids_reverse_mappings,
                    import_unique_ids_mappings=import_unique_ids_mappings,
                    ret_dict=ret_dict,
                    batch_size=batch_size,
                    # session=session
                )

            # store all pks to add to import group
            pks_for_group: List[int] = [
                foreign_ids_reverse_mappings[NODE_ENTITY_NAME][v['uuid']]
                for entries in [existing_entries, new_entries]
                for v in entries.get(NODE_ENTITY_NAME, {}).values()
            ]

            # now delete the entity data because we no longer need it
            del existing_entries
            del new_entries

            IMPORT_LOGGER.debug('STORING NODE LINKS...')
            _store_node_links(
                reader=reader,
                ignore_unknown_nodes=ignore_unknown_nodes,
                foreign_ids_reverse_mappings=foreign_ids_reverse_mappings,
                ret_dict=ret_dict,
                batch_size=batch_size,
                # session=session
            )

            IMPORT_LOGGER.debug('STORING GROUP ELEMENTS...')
            _add_nodes_to_groups(
                group_count=reader.entity_count(GROUP_ENTITY_NAME),
                group_uuids=reader.iter_group_uuids(),
                foreign_ids_reverse_mappings=foreign_ids_reverse_mappings
            )

        ######################################
        # Put everything in a specific group #
        ######################################
        # Note this is done in a separate transaction
        group = _make_import_group(group=group, node_pks=pks_for_group)

    # Summarize import
    result_summary(ret_dict, getattr(group, 'label', None))

    return ret_dict


def _select_entity_data(
    *, entity_name: str, reader: ArchiveReaderAbstract, new_entries: Dict[str, Dict[str, dict]],
    existing_entries: Dict[str, Dict[str, dict]], foreign_ids_reverse_mappings: Dict[str, Dict[str, int]],
    extras_mode_new: str
):
    """Select the data to import by comparing the AiiDA database to the archive contents."""
    cls_signature = entity_names_to_signatures[entity_name]
    model = get_object_from_string(cls_signature)
    unique_identifier = reader.metadata.unique_identifiers.get(entity_name, None)

    # Not necessarily all models are present in the archive
    if entity_name not in reader.entity_names:
        return

    existing_entries.setdefault(entity_name, {})
    new_entries.setdefault(entity_name, {})

    if unique_identifier is None:
        new_entries[entity_name] = {str(pk): fields for pk, fields in reader.iter_entity_fields(entity_name)}
        return

    # skip nodes that are already present in the DB
    import_unique_ids = set(
        f[unique_identifier] for _, f in reader.iter_entity_fields(entity_name, fields=(unique_identifier,))
    )

    relevant_db_entries = {}
    if import_unique_ids:
        relevant_db_entries_result = model.objects.filter(**{f'{unique_identifier}__in': import_unique_ids})
        if relevant_db_entries_result.count():
            with get_progress_reporter()(
                desc=f'Finding existing entities - {entity_name}', total=relevant_db_entries_result.count()
            ) as progress:
                # Imitating QueryBuilder.iterall() with default settings
                for object_ in relevant_db_entries_result.iterator(chunk_size=100):
                    progress.update()
                    # Note: UUIDs need to be converted to strings
                    relevant_db_entries.update({str(getattr(object_, unique_identifier)): object_})

    foreign_ids_reverse_mappings[entity_name] = {k: v.pk for k, v in relevant_db_entries.items()}

    entity_count = reader.entity_count(entity_name)
    if not entity_count:
        return

    with get_progress_reporter()(desc=f'Reading archived entities - {entity_name}', total=entity_count) as progress:
        imported_comp_names = set()
        for pk, fields in reader.iter_entity_fields(entity_name):
            progress.update()
            if entity_name == GROUP_ENTITY_NAME:
                # Check if there is already a group with the same name
                dupl_counter = 0
                orig_label = fields['label']
                while model.objects.filter(label=fields['label']):
                    fields['label'] = orig_label + DUPL_SUFFIX.format(dupl_counter)
                    dupl_counter += 1
                    if dupl_counter == MAX_GROUPS:
                        raise exceptions.ImportUniquenessError(
                            f'A group of that label ( {orig_label} ) already exists and I could not create a new one'
                        )

            elif entity_name == COMPUTER_ENTITY_NAME:
                # Check if there is already a computer with the same name in the database
                dupl = (model.objects.filter(name=fields['name']) or fields['name'] in imported_comp_names)
                orig_name = fields['name']
                dupl_counter = 0
                while dupl:
                    # Rename the new computer
                    fields['name'] = orig_name + DUPL_SUFFIX.format(dupl_counter)
                    dupl = (model.objects.filter(name=fields['name']) or fields['name'] in imported_comp_names)
                    dupl_counter += 1
                    if dupl_counter == MAX_COMPUTERS:
                        raise exceptions.ImportUniquenessError(
                            f'A computer of that name ( {orig_name} ) already exists and I could not create a new one'
                        )

                imported_comp_names.add(fields['name'])

            if fields[unique_identifier] in relevant_db_entries:
                # Already in DB
                existing_entries[entity_name][str(pk)] = fields
            else:
                # To be added
                if entity_name == NODE_ENTITY_NAME:
                    # format extras
                    fields = _sanitize_extras(fields)
                    # strip checkpoints
                    fields = _strip_checkpoints(fields)
                    if extras_mode_new != 'import':
                        fields.pop('extras', None)
                new_entries[entity_name][str(pk)] = fields


def _store_entity_data(
    *, reader: ArchiveReaderAbstract, entity_name: str, comment_mode: str, extras_mode_existing: str,
    new_entries: Dict[str, Dict[str, dict]], existing_entries: Dict[str, Dict[str, dict]],
    foreign_ids_reverse_mappings: Dict[str, Dict[str, int]], import_unique_ids_mappings: Dict[str, Dict[int, str]],
    ret_dict: dict, batch_size: int
):
    """Store the entity data on the AiiDA profile."""
    from aiida.backends.djsite.db import models

    cls_signature = entity_names_to_signatures[entity_name]
    model = get_object_from_string(cls_signature)
    fields_info = reader.metadata.all_fields_info.get(entity_name, {})
    unique_identifier = reader.metadata.unique_identifiers.get(entity_name, None)

    pbar_base_str = f'{entity_name}s - '

    # EXISTING ENTRIES
    if existing_entries[entity_name]:

        with get_progress_reporter()(
            total=len(existing_entries[entity_name]), desc=f'{pbar_base_str} existing entries'
        ) as progress:

            for import_entry_pk, entry_data in existing_entries[entity_name].items():

                progress.update()

                unique_id = entry_data[unique_identifier]
                existing_entry_id = foreign_ids_reverse_mappings[entity_name][unique_id]
                import_data = dict(
                    deserialize_field(
                        k,
                        v,
                        fields_info=fields_info,
                        import_unique_ids_mappings=import_unique_ids_mappings,
                        foreign_ids_reverse_mappings=foreign_ids_reverse_mappings
                    ) for k, v in entry_data.items()
                )
                # TODO COMPARE, AND COMPARE ATTRIBUTES

                if model is models.DbComment:
                    new_entry_uuid = merge_comment(import_data, comment_mode)
                    if new_entry_uuid is not None:
                        entry_data[unique_identifier] = new_entry_uuid
                        new_entries[entity_name][import_entry_pk] = entry_data

                if entity_name not in ret_dict:
                    ret_dict[entity_name] = {'new': [], 'existing': []}
                ret_dict[entity_name]['existing'].append((import_entry_pk, existing_entry_id))

                # print('  `-> WARNING: NO DUPLICITY CHECK DONE!')
                # CHECK ALSO FILES!

    # Store all objects for this model in a list, and store them all in once at the end.
    objects_to_create = []
    # This is needed later to associate the import entry with the new pk
    import_new_entry_pks = {}

    # NEW ENTRIES
    for import_entry_pk, entry_data in new_entries[entity_name].items():
        unique_id = entry_data[unique_identifier]
        import_data = dict(
            deserialize_field(
                k,
                v,
                fields_info=fields_info,
                import_unique_ids_mappings=import_unique_ids_mappings,
                foreign_ids_reverse_mappings=foreign_ids_reverse_mappings
            ) for k, v in entry_data.items()
        )

        objects_to_create.append(model(**import_data))
        import_new_entry_pks[unique_id] = import_entry_pk

    if entity_name == NODE_ENTITY_NAME:

        # Before storing entries in the DB, I store the files (if these are nodes).
        # Note: only for new entries!
        uuids_to_create = [obj.uuid for obj in objects_to_create]
        _copy_node_repositories(uuids_to_create=uuids_to_create, reader=reader)

        # For the existing nodes that are also in the imported list we also update their extras if necessary
        if existing_entries[entity_name]:

            with get_progress_reporter()(
                total=len(existing_entries[entity_name]), desc='Updating existing node extras'
            ) as progress:

                import_existing_entry_pks = {
                    entry_data[unique_identifier]: import_entry_pk
                    for import_entry_pk, entry_data in existing_entries[entity_name].items()
                }
                for node in models.DbNode.objects.filter(uuid__in=import_existing_entry_pks).all():  # pylint: disable=no-member
                    import_entry_uuid = str(node.uuid)
                    import_entry_pk = import_existing_entry_pks[import_entry_uuid]

                    pbar_node_base_str = f"{pbar_base_str}UUID={import_entry_uuid.split('-')[0]} - "
                    progress.set_description_str(f'{pbar_node_base_str}Extras', refresh=False)
                    progress.update()

                    old_extras = node.extras.copy()
                    extras = existing_entries[entity_name][str(import_entry_pk)].get('extras', {})

                    new_extras = merge_extras(node.extras, extras, extras_mode_existing)

                    if new_extras != old_extras:
                        # Already saving existing node here to update its extras
                        node.extras = new_extras
                        node.save()

    if not objects_to_create:
        return

    with get_progress_reporter()(total=len(objects_to_create), desc=f'{pbar_base_str} storing new') as progress:

        # If there is an mtime in the field, disable the automatic update
        # to keep the mtime that we have set here
        if 'mtime' in [field.name for field in model._meta.local_fields]:
            with models.suppress_auto_now([(model, ['mtime'])]):
                # Store them all in once; however, the PK are not set in this way...
                model.objects.bulk_create(objects_to_create, batch_size=batch_size)
        else:
            model.objects.bulk_create(objects_to_create, batch_size=batch_size)

        # Get back the just-saved entries
        just_saved_queryset = model.objects.filter(**{
            f'{unique_identifier}__in': import_new_entry_pks.keys()
        }).values_list(unique_identifier, 'pk')
        # note: convert uuids from type UUID to strings
        just_saved = {str(key): value for key, value in just_saved_queryset}

        # Now I have the PKs, print the info
        # Moreover, add newly created Nodes to foreign_ids_reverse_mappings
        for unique_id, new_pk in just_saved.items():
            import_entry_pk = import_new_entry_pks[unique_id]
            foreign_ids_reverse_mappings[entity_name][unique_id] = new_pk
            if entity_name not in ret_dict:
                ret_dict[entity_name] = {'new': [], 'existing': []}
            ret_dict[entity_name]['new'].append((import_entry_pk, new_pk))

            progress.update()
            # TODO prints too many lines
            # IMPORT_LOGGER.debug(f'New {entity_name}: {unique_id} ({import_entry_pk}->{new_pk})')


def _store_node_links(
    *,
    reader: ArchiveReaderAbstract,
    ignore_unknown_nodes: bool,
    foreign_ids_reverse_mappings: Dict[str, Dict[str, int]],
    ret_dict: dict,
    batch_size: int,
):
    """Store node links to the database."""
    from aiida.backends.djsite.db import models

    links_to_store = []

    # Needed, since QueryBuilder does not yet work for recently saved Nodes
    existing_links = {
        (l[0], l[1], l[2], l[3]) for l in models.DbLink.objects.all().values_list('input', 'output', 'label', 'type')
    }
    existing_outgoing_unique = {(l[0], l[3]) for l in existing_links}
    existing_outgoing_unique_pair = {(l[0], l[2], l[3]) for l in existing_links}
    existing_incoming_unique = {(l[1], l[3]) for l in existing_links}
    existing_incoming_unique_pair = {(l[1], l[2], l[3]) for l in existing_links}

    calculation_node_types = 'process.calculation.'
    workflow_node_types = 'process.workflow.'
    data_node_types = 'data.'

    link_mapping = {
        LinkType.CALL_CALC: (workflow_node_types, calculation_node_types, 'unique_triple', 'unique'),
        LinkType.CALL_WORK: (workflow_node_types, workflow_node_types, 'unique_triple', 'unique'),
        LinkType.CREATE: (calculation_node_types, data_node_types, 'unique_pair', 'unique'),
        LinkType.INPUT_CALC: (data_node_types, calculation_node_types, 'unique_triple', 'unique_pair'),
        LinkType.INPUT_WORK: (data_node_types, workflow_node_types, 'unique_triple', 'unique_pair'),
        LinkType.RETURN: (workflow_node_types, data_node_types, 'unique_pair', 'unique_triple'),
    }

    link_count = reader.link_count

    if not link_count:
        IMPORT_LOGGER.debug('   (0 new links...)')
        return

    pbar_base_str = 'Links - '
    with get_progress_reporter()(total=link_count, desc=pbar_base_str) as progress_bar:

        for link in reader.iter_link_data():

            progress_bar.set_description_str(f"{pbar_base_str}label={link['label']}", refresh=False)
            progress_bar.update()

            # Check for dangling Links within the, supposed, self-consistent archive
            try:
                in_id = foreign_ids_reverse_mappings[NODE_ENTITY_NAME][link['input']]
                out_id = foreign_ids_reverse_mappings[NODE_ENTITY_NAME][link['output']]
            except KeyError:
                if ignore_unknown_nodes:
                    continue
                raise exceptions.ImportValidationError(
                    'Trying to create a link with one or both unknown nodes, stopping (in_uuid={}, out_uuid={}, '
                    'label={}, type={})'.format(link['input'], link['output'], link['label'], link['type'])
                )

            # Check if link already exists, skip if it does
            # This is equivalent to an existing triple link (i.e. unique_triple from below)
            if (in_id, out_id, link['label'], link['type']) in existing_links:
                continue

            # Since backend specific Links (DbLink) are not validated upon creation, we will now validate them.
            try:
                validate_link_label(link['label'])
            except ValueError as why:
                raise exceptions.ImportValidationError(f'Error during Link label validation: {why}')

            source = models.DbNode.objects.get(id=in_id)
            target = models.DbNode.objects.get(id=out_id)

            if source.uuid == target.uuid:
                raise exceptions.ImportValidationError('Cannot add a link to oneself')

            link_type = LinkType(link['type'])
            type_source, type_target, outdegree, indegree = link_mapping[link_type]

            # Check if source Node is a valid type
            if not source.node_type.startswith(type_source):
                raise exceptions.ImportValidationError(
                    f'Cannot add a {link_type} link from {source.node_type} to {target.node_type}'
                )

            # Check if target Node is a valid type
            if not target.node_type.startswith(type_target):
                raise exceptions.ImportValidationError(
                    f'Cannot add a {link_type} link from {source.node_type} to {target.node_type}'
                )

            # If the outdegree is `unique` there cannot already be any other outgoing link of that type,
            # i.e., the source Node may not have a LinkType of current LinkType, going out, existing already.
            if outdegree == 'unique' and (in_id, link['type']) in existing_outgoing_unique:
                raise exceptions.ImportValidationError(f'Node<{source.uuid}> already has an outgoing {link_type} link')

            # If the outdegree is `unique_pair`,
            # then the link labels for outgoing links of this type should be unique,
            # i.e., the source Node may not have a LinkType of current LinkType, going out,
            # that also has the current Link label, existing already.
            elif outdegree == 'unique_pair' and \
            (in_id, link['label'], link['type']) in existing_outgoing_unique_pair:
                raise exceptions.ImportValidationError(
                    f"Node<{source.uuid}> already has an outgoing {link_type} link with label \"{link['label']}\""
                )

            # If the indegree is `unique` there cannot already be any other incoming links of that type,
            # i.e., the target Node may not have a LinkType of current LinkType, coming in, existing already.
            if indegree == 'unique' and (out_id, link['type']) in existing_incoming_unique:
                raise exceptions.ImportValidationError(f'Node<{target.uuid}> already has an incoming {link_type} link')

            # If the indegree is `unique_pair`,
            # then the link labels for incoming links of this type should be unique,
            # i.e., the target Node may not have a LinkType of current LinkType, coming in
            # that also has the current Link label, existing already.
            elif indegree == 'unique_pair' and \
            (out_id, link['label'], link['type']) in existing_incoming_unique_pair:
                raise exceptions.ImportValidationError(
                    f"Node<{target.uuid}> already has an incoming {link_type} link with label \"{link['label']}\""
                )

            # New link
            links_to_store.append(
                models.DbLink(input_id=in_id, output_id=out_id, label=link['label'], type=link['type'])
            )
            if 'Link' not in ret_dict:
                ret_dict['Link'] = {'new': []}
            ret_dict['Link']['new'].append((in_id, out_id))

            # Add new Link to sets of existing Links 'input PK', 'output PK', 'label', 'type'
            existing_links.add((in_id, out_id, link['label'], link['type']))
            existing_outgoing_unique.add((in_id, link['type']))
            existing_outgoing_unique_pair.add((in_id, link['label'], link['type']))
            existing_incoming_unique.add((out_id, link['type']))
            existing_incoming_unique_pair.add((out_id, link['label'], link['type']))

    # Store new links
    if links_to_store:
        IMPORT_LOGGER.debug('   (%d new links...)', len(links_to_store))

        models.DbLink.objects.bulk_create(links_to_store, batch_size=batch_size)
    else:
        IMPORT_LOGGER.debug('   (0 new links...)')


def _add_nodes_to_groups(
    *, group_count: int, group_uuids: Iterable[Tuple[str, Set[str]]], foreign_ids_reverse_mappings: Dict[str, Dict[str,
                                                                                                                   int]]
):
    """Add nodes to imported groups."""
    from aiida.backends.djsite.db import models

    if not group_count:
        return

    pbar_base_str = 'Groups - '

    with get_progress_reporter()(total=group_count, desc=pbar_base_str) as progress:
        for groupuuid, groupnodes in group_uuids:
            if not groupnodes:
                progress.update()
                continue
            # TODO: cache these to avoid too many queries
            group_ = models.DbGroup.objects.get(uuid=groupuuid)

            progress.set_description_str(f'{pbar_base_str}label={group_.label}', refresh=False)
            progress.update()

            nodes_to_store = [foreign_ids_reverse_mappings[NODE_ENTITY_NAME][node_uuid] for node_uuid in groupnodes]
            if nodes_to_store:
                group_.dbnodes.add(*nodes_to_store)
