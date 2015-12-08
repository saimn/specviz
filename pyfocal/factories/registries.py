import yaml
import os


class Registry(object):
    """
    Maintains a set of objects.
    """
    def __init__(self):
        self._members = []


class CustomLoaderRegistry(Registry):
    def __init__(self):
        super(CustomLoaderRegistry, self).__init__()

        cur_path = os.path.join(os.path.dirname(__file__), 'default_loaders')

        for file_name in os.listdir(cur_path):
            f_path = os.path.join(cur_path, file_name)
            custom_loader = yaml.load(file(f_path, 'r'))
            custom_loader.set_filter()

            self._members.append(custom_loader)

    def get(self, filter):
        return [x for x in self._members if x.filter == filter][0]

    @property
    def filters(self):
        return [x.filter for x in self._members]


class YAMLLoader(yaml.YAMLObject):
    yaml_tag = u'!CustomLoader'

    def __init__(self, extension, name, data, uncertainty, mask, meta):
        self.name = name
        self.extension = extension
        self.data = data
        self.uncertainty = uncertainty
        self.mask = mask
        self.meta = meta
        self.filter = None

    def set_filter(self):
        if isinstance(self.extension, list):
            filter_string = ' '.join(['*.{}'.format(x)
                                       for x in self.extension])
            self.filter = "{} ({})".format(self.name, filter_string)
        else:
            self.filter = "{} (*.{})".format(self.name, self.extension)


loader_factory = CustomLoaderRegistry()